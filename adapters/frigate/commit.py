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
from collections import namedtuple

from frigate_client import FrigateClient, FrigateError

# Structured plan from plan_actions(). Named so future additions (more action
# buckets) don't break callers — refer to fields by name, not position.
Plan = namedtuple("Plan", [
    "cands", "categorize", "face_classify", "deletes", "train_deletes",
    "lib_face_moves", "lib_face_dels", "lib_data_moves", "lib_data_dels",
    "rescan_registers", "noop",
])

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


# How many crops to KEEP per event (the rest are deleted from the train pool).
# Diversity over volume — Frigate's "cap similar" guidance (ADR-0015 / Tenet 4).
KEEP_PER_EVENT = max(1, int(os.environ.get("WINNOW_KEEP_PER_EVENT", "3")))


def _capture_ts(f):
    """Capture instant from a train filename <ts>-<rand>-<ts2>-<label>-<score>.ext."""
    try:
        return float(f.rsplit(".", 1)[0].split("-")[2])
    except (ValueError, IndexError):
        return 0.0


def _keep_subset(files, n=None):
    """Up to n crops spread across the event's timeline — genuinely different frames
    (car arriving / parked / leaving), not n near-identical best-angle shots. Evenly
    spaced picks include the first and last. n defaults to KEEP_PER_EVENT."""
    n = KEEP_PER_EVENT if n is None else n
    files = sorted(files, key=_capture_ts)
    if len(files) <= n:
        return list(files)
    if n == 1:
        return [files[len(files) // 2]]
    idx = sorted({round(i * (len(files) - 1) / (n - 1)) for i in range(n)})
    return [files[i] for i in idx]


def plan_actions():
    """Return (cands, categorize, face_classify, deletes, train_deletes, noop).
      categorize     -> [(cand, category, training_file)]  (one per kept crop)
      train_deletes  -> [(cand, [files])]                   (redundant per-event siblings)
      deletes        -> [cand]                              (rejected faces)
    """
    cands = {c["cid"]: c for c in _load_jsonl(os.path.join(REVIEW, "candidates.jsonl"))}
    verdicts = _verdicts()
    committed = _committed()
    categorize, face_classify, deletes, train_deletes, noop = [], [], [], [], []
    # Library-cleanup buckets (ADR-0016): committed items the reviewer is curating.
    # Different APIs from the train-pool path: face library uses face_reclassify
    # (one-shot or register+delete) + delete_faces; dataset library uses
    # reclassify (in-model move) + delete_dataset_images.
    rescan_registers: list = []    # (cand, target_name) — face_register the event snapshot
    lib_face_moves: list = []      # (cand, new_name)
    lib_face_dels: list = []       # cand
    lib_data_moves: list = []      # (cand, new_category)
    lib_data_dels: list = []       # cand
    for cid, verdict in verdicts.items():
        if cid in committed:
            continue
        c = cands.get(cid)
        if not c:
            continue
        target = (verdict[len("assign:"):] if isinstance(verdict, str)
                  and verdict.startswith("assign:") else None)
        # ---- LIBRARY cleanup (ADR-0016) ------------------------------------
        # Branch first so library candidates never fall through to the train-pool
        # logic below (which would try to categorize a non-existent training_file).
        if c.get("source") == "library_face":
            if verdict == "reject":
                lib_face_dels.append(c)
            elif target is not None and target != c["identity"]:
                lib_face_moves.append((c, target))
            else:
                noop.append(cid)         # yes/no/skip/self-assign = just a confirmation
            continue
        # ---- RESCAN (ADR-0017): yes/reassign -> face_register the event snapshot ----
        if c.get("source") == "rescan":
            if verdict == "reject":
                noop.append(cid)             # rejected = don't add to library; just drop
            elif verdict == "yes":
                rescan_registers.append((c, c["rescan_name"]))   # confirm Frigate's guess
            elif target is not None and target != c["identity"]:
                rescan_registers.append((c, target))             # reassign to actual person
            else:
                noop.append(cid)             # skip / self-assign / unknown
            continue
        if c.get("source") == "library_dataset":
            if verdict == "reject":
                lib_data_dels.append(c)
            elif target is not None and target != c["identity"]:
                lib_data_moves.append((c, target))
            else:
                noop.append(cid)
            continue
        # "reject" (faces): delete the crop from the face library. Classifier reject -> noop.
        if verdict == "reject":
            (deletes if (c["kind"] == "person" and c.get("face_train")) else noop).append(
                c if (c["kind"] == "person" and c.get("face_train")) else cid)
            continue
        # faces: classify the guessed name (yes) or the reassigned target
        if c["kind"] == "person" and c.get("face_train"):
            if target is not None:
                face_classify.append((c, target))
            elif verdict == "yes":
                face_classify.append((c, c["identity"]))
            else:
                noop.append(cid)             # 'no' = not this person; leave/park
            continue
        # classifiers: settle on the class this verdict commits to
        if target is not None:
            cls = target                      # reassigned (incl. 'none')
        elif verdict == "yes":
            cls = "none" if c.get("role") == "negative" else c["identity"]
        elif c.get("choices") and verdict in c["choices"]:
            cls = verdict                     # legacy N-way choices
        else:
            noop.append(cid)                  # 'no' / 'skip' / unknown -> leave it
            continue
        # EVENT-level (ADR-0015): keep a diverse subset as `cls`, delete the rest.
        # Prefer the set computed at build time (what the reviewer SAW in the
        # lightbox); fall back to recomputing if an older candidate lacks it.
        if c.get("event_files") and c.get("model"):
            keep = c.get("keep_files") or _keep_subset(c["event_files"])
            for f in keep:
                categorize.append((c, cls, f))
            rest = [f for f in c["event_files"] if f not in keep]
            if rest:
                train_deletes.append((c, rest))
        elif c.get("model"):                  # legacy per-crop classifier (e.g. AI mode)
            categorize.append((c, cls, c["training_file"]))
        else:
            noop.append(cid)
    return Plan(cands=cands, categorize=categorize, face_classify=face_classify,
                deletes=deletes, train_deletes=train_deletes,
                lib_face_moves=lib_face_moves, lib_face_dels=lib_face_dels,
                lib_data_moves=lib_data_moves, lib_data_dels=lib_data_dels,
                rescan_registers=rescan_registers, noop=noop)


def run(apply=False, do_train=True, client=None):
    p = plan_actions()
    cands = p.cands; categorize = p.categorize; face_classify = p.face_classify
    deletes = p.deletes; train_deletes = p.train_deletes
    lib_face_moves = p.lib_face_moves; lib_face_dels = p.lib_face_dels
    lib_data_moves = p.lib_data_moves; lib_data_dels = p.lib_data_dels
    rescan_registers = p.rescan_registers
    noop = p.noop
    n_tdel_planned = sum(len(files) for _, files in train_deletes)
    n_lib = (len(lib_face_moves) + len(lib_face_dels) + len(lib_data_moves) + len(lib_data_dels))
    print(f"plan: {len(categorize)} categorize, {len(face_classify)} face-assign, "
          f"{len(deletes)} face-delete, {n_tdel_planned} redundant-frame delete, "
          f"{n_lib} library cleanup ({len(lib_face_moves)+len(lib_data_moves)} reassign / "
          f"{len(lib_face_dels)+len(lib_data_dels)} delete), "
          f"{len(rescan_registers)} rescan-register, {len(noop)} no-op")
    for c, cat, tf in categorize[:8]:
        print(f"  categorize  {c['model']}/{cat:14} <- {tf[:40]}")
    if len(categorize) > 8:
        print(f"  ... +{len(categorize)-8} more")
    for c, name in face_classify[:8]:
        print(f"  assign face {name} <- {c['face_train'][:40]}")
    for c, files in train_deletes[:8]:
        print(f"  prune {len(files)} redundant frames from {c['model']} "
              f"(event {c.get('meta', {}).get('event', '?')})")

    if not apply:
        print("\n(dry run — re-run with --apply to execute)")
        return {"categorized": 0, "faces": 0, "deleted": 0, "train_deleted": 0,
                "lib_reassigned": 0, "lib_deleted": 0, "rescan_registered": 0, "trained": []}

    c = client or FrigateClient()
    c.login()

    # group every action by candidate so each event commits ATOMICALLY + idempotently:
    # we only mark a cid committed if all of its categorize/delete/classify calls succeed.
    from collections import defaultdict
    plan = defaultdict(lambda: {"cand": None, "cat": [], "tdel": [],
                                "face": None, "delface": False,
                                "lib_move": None, "lib_del": False,
                                "rescan_target": None})
    for cand, cat, tf in categorize:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["cat"].append((cat, tf))
    for cand, files in train_deletes:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["tdel"] = files
    for cand, name in face_classify:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["face"] = name
    for cand in deletes:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["delface"] = True
    # library cleanup (ADR-0016) — one move or one delete per cid
    for cand, new_name in lib_face_moves + lib_data_moves:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["lib_move"] = new_name
    for cand in lib_face_dels + lib_data_dels:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["lib_del"] = True
    # rescan (ADR-0017) — one face_register call per confirmed candidate
    for cand, target in rescan_registers:
        plan[cand["cid"]]["cand"] = cand
        plan[cand["cid"]]["rescan_target"] = target

    done = open(os.path.join(REVIEW, "committed.jsonl"), "a")
    models = set()
    n_cat = n_face = n_del = n_tdel = n_lib_move = n_lib_del = n_rescan = 0
    for cid, p in plan.items():
        cand, ok = p["cand"], True
        for cat, tf in p["cat"]:
            try:
                c.categorize(cand["model"], cat, tf); n_cat += 1
            except FrigateError as e:
                print(f"  ! categorize failed {cid}: {e}"); ok = False
        if p["tdel"]:
            try:
                c.delete_train(cand["model"], p["tdel"]); n_tdel += len(p["tdel"])
            except FrigateError as e:
                print(f"  ! prune failed {cid}: {e}"); ok = False
        if p["face"]:
            try:
                c.classify_face_train(p["face"], cand["face_train"]); n_face += 1
            except FrigateError as e:
                print(f"  ! face assign failed {cid}: {e}"); ok = False
        if p["delface"]:
            try:
                c.delete_faces("train", [cand["face_train"]]); n_del += 1
            except FrigateError as e:
                print(f"  ! face delete failed {cid}: {e}"); ok = False
        # ---- LIBRARY cleanup (ADR-0016): move or delete in the COMMITTED pools ----
        if p["lib_move"]:
            try:
                if cand.get("source") == "library_face":
                    # adaptive: one-shot reclassify on v0.18+, register+delete on v0.17
                    c.face_reclassify(cand["face_lib_name"], cand["library_id"], p["lib_move"])
                else:                                  # library_dataset
                    c.reclassify(cand["model"], cand["library_category"],
                                 cand["library_id"], p["lib_move"])
                    models.add(cand["model"])           # retrain the affected classifier
                n_lib_move += 1
            except (FrigateError, Exception) as e:
                print(f"  ! library reassign failed {cid}: {e}"); ok = False
        if p["lib_del"]:
            try:
                if cand.get("source") == "library_face":
                    c.delete_faces(cand["face_lib_name"], [cand["library_id"]])
                else:                                  # library_dataset
                    c.delete_dataset_images(cand["model"], cand["library_category"],
                                            [cand["library_id"]])
                    models.add(cand["model"])
                n_lib_del += 1
            except (FrigateError, Exception) as e:
                print(f"  ! library delete failed {cid}: {e}"); ok = False
        # ---- RESCAN register (ADR-0017 v2): face_register the saved CROP bytes ----
        # Crop bytes = single-body, single-face crop saved by rescan_recordings.py.
        # What the human verified in the lightbox IS what trains. Legacy v1
        # candidates (no rescan_crop_path) fall back to event-snapshot for
        # backward compat with any rescan_candidates.jsonl rows written pre-v2.
        if p["rescan_target"]:
            try:
                cp = cand.get("rescan_crop_path")
                if cp and os.path.exists(cp):
                    with open(cp, "rb") as f:
                        snap = f.read()
                    filename = os.path.basename(cp)
                else:
                    eid = cand["rescan_event_id"]
                    snap = c.fetch_event_snapshot(eid)
                    filename = f"{eid}.jpg"
                c.face_register(p["rescan_target"], snap, filename=filename)
                n_rescan += 1
            except (FrigateError, Exception) as e:
                print(f"  ! rescan register failed {cid}: {e}"); ok = False
        if cand.get("model"):
            models.add(cand["model"])
        if ok:
            done.write(json.dumps({"cid": cid, "ts": time.time()}) + "\n"); done.flush()

    for cid in noop:
        done.write(json.dumps({"cid": cid, "action": "noop", "ts": time.time()}) + "\n")
    done.close()

    trained = []
    if do_train and models:
        for m in sorted(models):
            try:
                c.train(m); trained.append(m); print(f"  triggered retrain: {m}")
            except FrigateError as e:
                print(f"  ! train failed for {m}: {e}")

    print(f"\ncommitted: {n_cat} categorized, {n_face} faces assigned, {n_del} faces "
          f"deleted, {n_tdel} redundant frames pruned, {n_lib_move} library reassigned, "
          f"{n_lib_del} library deleted, {n_rescan} rescan-registered, "
          f"retrained {trained or 'nothing'}")
    return {"categorized": n_cat, "faces": n_face, "deleted": n_del,
            "train_deleted": n_tdel, "lib_reassigned": n_lib_move,
            "rescan_registered": n_rescan,
            "lib_deleted": n_lib_del, "trained": trained}


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
