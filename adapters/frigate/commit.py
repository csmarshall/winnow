#!/usr/bin/env python3
"""Commit reviewed verdicts to Frigate via its API — batched and idempotent.

Reads review/verdicts.jsonl + candidates.jsonl and pushes the human decisions
through Frigate's official endpoints (ADR-0004):
  * confirmed classifier positive -> categorize into dataset/<identity>
  * confirmed classifier "other"  -> categorize into dataset/none (hard negative)
  * rejected face                 -> delete from the face library
  * then one retrain per affected model
`categorize` moves the file out of train/, so a committed image won't resurface.

Idempotent: every action is logged to review/committed.jsonl and skipped next
run. Safe by default — prints the plan and does nothing unless --apply is given.
Importable: a daemon can call run(apply=True) directly.

Usage:
  python3 commit.py                 # dry-run: show the plan
  python3 commit.py --apply         # execute + retrain
  python3 commit.py --apply --no-train
"""
from __future__ import annotations

import argparse
import json
import os
import time

from frigate_client import FrigateClient, FrigateError

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW = os.path.join(HERE, "..", "..", "review")


def _load_jsonl(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def _verdicts():
    v = {}
    for d in _load_jsonl(os.path.join(REVIEW, "verdicts.jsonl")):
        if d.get("verdict") == "__undo__":
            v.pop(d["cid"], None)
        else:
            v[d["cid"]] = d["verdict"]
    return v


def _committed():
    return {d["cid"] for d in _load_jsonl(os.path.join(REVIEW, "committed.jsonl"))}


def plan_actions():
    cands = {c["cid"]: c for c in _load_jsonl(os.path.join(REVIEW, "candidates.jsonl"))}
    verdicts = _verdicts()
    committed = _committed()
    categorize, face_classify, deletes, noop = [], [], [], []
    for cid, verdict in verdicts.items():
        if cid in committed:
            continue
        c = cands.get(cid)
        if not c:
            continue
        # "reject / not one of ours" (ADR-0014): delete the face from the source so
        # it stops being re-suggested. (Faces only — classifier rejects fall to noop.)
        if verdict == "reject":
            if c["kind"] == "person" and c.get("face_train"):
                deletes.append(c)
            else:
                noop.append(cid)
            continue
        # "?" reassignment: verdict is "assign:<target>" — send the image to that
        # subtype/person regardless of which pool it was reviewed in. A target that
        # doesn't exist yet is created by Frigate on categorize/classify.
        if isinstance(verdict, str) and verdict.startswith("assign:"):
            target = verdict[len("assign:"):]
            if c["kind"] == "person" and c.get("face_train"):
                face_classify.append((c, target))
            elif c.get("model"):
                categorize.append((c, target))
            else:
                noop.append(cid)
        elif c["kind"] == "person":
            if verdict == "yes" and c.get("face_train"):
                face_classify.append((c, c["identity"]))   # confirm guessed name
            else:
                noop.append(cid)             # 'no' = not this person; leave in pool
        elif c.get("choices"):           # manual assignment: the verdict IS the subtype
            if verdict in c["choices"]:
                categorize.append((c, verdict))
            else:
                noop.append(cid)         # 'skip' / anything else
        else:                            # classifier confirm (yes/no)
            if verdict == "yes":
                category = "none" if c.get("role") == "negative" else c["identity"]
                categorize.append((c, category))
            # a 'no' leaves the image (don't categorize); recorded so not re-shown.
            else:
                noop.append(cid)
    return cands, categorize, face_classify, deletes, noop


def run(apply=False, do_train=True, client=None):
    cands, categorize, face_classify, deletes, noop = plan_actions()
    print(f"plan: {len(categorize)} categorize, {len(face_classify)} face-assign, "
          f"{len(deletes)} delete, {len(noop)} no-op")
    for c, cat in categorize[:8]:
        print(f"  categorize  {c['model']}/{cat:14} <- {c['training_file'][:40]}")
    if len(categorize) > 8:
        print(f"  ... +{len(categorize)-8} more")
    for c, name in face_classify[:8]:
        print(f"  assign face {name} <- {c['face_train'][:40]}")

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        return {"categorized": 0, "faces": 0, "deleted": 0, "trained": []}

    c = client or FrigateClient()
    c.login()
    done = open(os.path.join(REVIEW, "committed.jsonl"), "a")
    models = set()
    n_cat = n_face = n_del = 0

    for cand, category in categorize:
        try:
            c.categorize(cand["model"], category, cand["training_file"])
            done.write(json.dumps({"cid": cand["cid"], "action": "categorize",
                                   "model": cand["model"], "category": category,
                                   "ts": time.time()}) + "\n")
            done.flush()
            models.add(cand["model"])
            n_cat += 1
        except FrigateError as e:
            print(f"  ! categorize failed for {cand['cid']}: {e}")

    for cand, name in face_classify:
        try:
            c.classify_face_train(name, cand["face_train"])
            done.write(json.dumps({"cid": cand["cid"], "action": "face_classify",
                                   "name": name, "ts": time.time()}) + "\n")
            done.flush()
            n_face += 1
        except FrigateError as e:
            print(f"  ! face assign failed for {cand['cid']}: {e}")

    for cand in deletes:
        try:
            c.delete_faces("train", [cand["face_train"]])   # remove the rejected face
            done.write(json.dumps({"cid": cand["cid"], "action": "delete_face",
                                   "file": cand["face_train"], "ts": time.time()}) + "\n")
            done.flush()
            n_del += 1
        except FrigateError as e:
            print(f"  ! delete failed for {cand['cid']}: {e}")

    for cid in noop:
        done.write(json.dumps({"cid": cid, "action": "noop", "ts": time.time()}) + "\n")
    done.close()

    trained = []
    if do_train and models:
        for m in sorted(models):
            try:
                c.train(m)
                trained.append(m)
                print(f"  triggered retrain: {m}")
            except FrigateError as e:
                print(f"  ! train failed for {m}: {e}")

    print(f"\ncommitted: {n_cat} categorized, {n_face} faces assigned, "
          f"{n_del} deleted, retrained {trained or 'nothing'}")
    return {"categorized": n_cat, "faces": n_face, "deleted": n_del, "trained": trained}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--no-train", action="store_true", help="skip the retrain")
    args = ap.parse_args()
    try:
        run(apply=args.apply, do_train=not args.no_train)
    except FrigateError as e:
        print(f"frigate error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
