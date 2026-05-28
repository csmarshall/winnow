#!/usr/bin/env python3
"""Promote human-confirmed review verdicts into Frigate's training data.

Reads review/verdicts.jsonl + review/candidates.jsonl and turns your swipe
decisions into stuff Frigate can actually train on. Because Frigate's data dirs
are root-owned, this NEVER writes into them directly — it stages everything into
a writable tree and emits a sudo install script you review and run.

Two distinct flows, because the data sources differ:

  CLASSIFIERS (dogs, cars): confirmed crops are NEW labeled examples. Staged as
    PNGs into Frigate's layout  clips/<model>/dataset/<Category>/<Cat>-<id>.png
    (target auto-detected by globbing existing dataset/<identity>/ dirs).

  FACES (people): candidates already live in clips/faces/<Name>/, so "yes" = keep
    (no-op) and "no" = a mislabeled library image to REMOVE. We emit a removal
    list (never auto-delete) for the install script to handle.

Outputs (all under promote/):
  staging/...              images ready to copy into Frigate
  plan.md                  human-readable summary of what will change
  install_to_frigate.sh    the sudo script you run to apply it

Usage:
  python3 src/promote.py                 # stage + generate plan & script
  python3 src/promote.py --min-conf 0.8  # only promote higher-confidence confirms
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import shutil

from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = os.path.join(HERE, "review")
PROMOTE = os.path.join(HERE, "promote")
STAGING = os.path.join(PROMOTE, "staging")
FRIGATE_CLIPS = "/opt/nvr/frigate/clips"

CLASSIFIER_KINDS = {"dog", "car"}

# Classifier models to retire (config block + data dir) during install:
#  - "Back Slider": dead state classifier.
#  - "Scrappy": redundant 2nd dog model — we consolidate to a single dog
#    classifier ("Scooby" model, categories Scooby/Scrappy/none). Confirmed Scrappy
#    crops go into clips/Scooby/dataset/Scrappy (the surviving model), so this dir
#    is only the leftover duplicate model.
RETIRE_MODELS = ["Back Slider", "Scrappy"]


def retire_classifier_sh(name: str) -> str:
    """Bash to drop a classifier's config block (indent-aware, backed up) and its
    clips/<name> data dir. Idempotent — no-op once gone."""
    return f'''
echo ">> retire {name!r} classifier (config block + data dir)"
python3 - <<'PYEOF'
import shutil, time
cfg = "/opt/sw/frigate/config/config.yml"
name = {name!r}
lines = open(cfg).read().split("\\n")
out, i, removed = [], 0, False
while i < len(lines):
    ln = lines[i]
    if (len(ln) - len(ln.lstrip())) == 4 and ln.strip() == name + ":":
        i += 1
        while i < len(lines) and (lines[i].strip() == "" or
                                  (len(lines[i]) - len(lines[i].lstrip())) > 4):
            i += 1
        removed = True
        continue
    out.append(ln)
    i += 1
if removed:
    shutil.copy(cfg, cfg + ".bak." + name.replace(" ", "_") + "-" + time.strftime("%F-%H%M%S"))
    open(cfg, "w").write("\\n".join(out))
    print("   removed " + name + " block from config.yml (backup made)")
else:
    print("   " + name + " not in config.yml (already gone)")
PYEOF
if [ -d "/opt/nvr/frigate/clips/{name}" ]; then
  rm -rfv "/opt/nvr/frigate/clips/{name}"
else
  echo "   clips/{name} data dir already gone"
fi
'''


def load():
    cands = {}
    for line in open(os.path.join(REVIEW, "candidates.jsonl")):
        c = json.loads(line)
        cands[c["cid"]] = c
    verdicts = {}
    vpath = os.path.join(REVIEW, "verdicts.jsonl")
    if os.path.exists(vpath):
        for line in open(vpath):
            v = json.loads(line)
            if v.get("verdict") == "__undo__":
                verdicts.pop(v["cid"], None)
            else:
                verdicts[v["cid"]] = v["verdict"]
    return cands, verdicts


def _ahash(path: str) -> int:
    """64-bit average hash of an image (for near-duplicate detection)."""
    im = Image.open(path).convert("L").resize((8, 8))
    px = list(im.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p > avg:
            bits |= 1 << i
    return bits


def diverse_sample(items: list[dict], cap: int | None, dup_dist: int) -> list[dict]:
    """Frigate's own guidance: diversity > volume. Drop near-duplicate crops
    (avg-hash Hamming <= dup_dist), then, if still over cap, spread the picks
    across cameras and hour-of-day so the training set is varied, not redundant."""
    kept, hashes = [], []
    for c in items:
        try:
            h = _ahash(c["img"])
        except Exception:
            kept.append(c)
            continue
        if any(bin(h ^ k).count("1") <= dup_dist for k in hashes):
            continue
        hashes.append(h)
        kept.append(c)
    if cap is None or len(kept) <= cap:
        return kept
    groups = collections.OrderedDict()
    for c in kept:
        m = c.get("meta") or {}
        key = (m.get("camera", "?"), (m.get("time") or "")[11:13])  # camera, hour
        groups.setdefault(key, []).append(c)
    out, glists = [], [list(g) for g in groups.values()]
    while len(out) < cap:
        progressed = False
        for g in glists:
            if g:
                out.append(g.pop())
                progressed = True
                if len(out) >= cap:
                    break
        if not progressed:
            break
    return out


def find_classifier_target(identity: str) -> str | None:
    """Locate Frigate's dataset category dir for an identity, e.g.
    clips/Scooby/dataset/Scooby/ or clips/Batmobile/dataset/Batmobile/."""
    hits = glob.glob(os.path.join(FRIGATE_CLIPS, "*", "dataset", identity))
    # never target a model dir we're about to retire (e.g. the duplicate Scrappy)
    hits = [h for h in hits if os.path.basename(
        os.path.dirname(os.path.dirname(h))) not in RETIRE_MODELS]
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-conf", type=float, default=0.0,
                    help="only promote confirmed crops with >= this model confidence")
    ap.add_argument("--cap", type=int, default=80,
                    help="max diverse examples per identity per cycle (diversity>volume)")
    ap.add_argument("--dedup", type=int, default=4,
                    help="drop crops within this avg-hash Hamming distance as near-dups")
    args = ap.parse_args()

    cands, verdicts = load()
    if os.path.isdir(STAGING):
        shutil.rmtree(STAGING)
    os.makedirs(STAGING, exist_ok=True)

    # collect decisions per (kind, identity)
    yes, no = {}, {}
    for cid, v in verdicts.items():
        c = cands.get(cid)
        if not c:
            continue
        key = (c["kind"], c["identity"])
        if v == "yes":
            yes.setdefault(key, []).append(c)
        elif v == "no":
            no.setdefault(key, []).append(c)

    plan = ["# Promotion plan", ""]
    install = ["#!/usr/bin/env bash",
               "# Generated by promote.py — review before running with sudo.",
               "set -euo pipefail", "unset TMOUT", ""]
    copies = removals = 0

    # none-class dirs per kind, derived from each kind's positive-identity models
    none_dirs: dict[str, set] = {}
    for c in cands.values():
        if c["kind"] in CLASSIFIER_KINDS and c.get("role", "positive") == "positive":
            t = find_classifier_target(c["identity"])
            if t:
                none_dirs.setdefault(c["kind"], set()).add(
                    os.path.join(os.path.dirname(t), "none"))

    def stage_pngs(stage_dir, prefix, items):
        os.makedirs(stage_dir, exist_ok=True)
        n = 0
        for c in items:
            dst = os.path.join(stage_dir, f"{prefix}-{os.path.basename(c['img'])[:-4]}.png")
            try:
                Image.open(c["img"]).convert("RGB").save(dst)
                n += 1
            except Exception as e:
                plan.append(f"  ! skip {c['img']}: {e}")
        return n

    # --- classifiers: positive examples -> dataset/<identity> (diverse-sampled) ---
    plan.append("## Classifiers — positive examples (diverse-sampled)\n")
    for (kind, identity), items in sorted(yes.items()):
        if kind not in CLASSIFIER_KINDS or items[0].get("role", "positive") != "positive":
            continue
        items = [c for c in items if (c.get("confidence") or 0) >= args.min_conf]
        if not items:
            continue
        before = len(items)
        items = diverse_sample(items, args.cap, args.dedup)
        target = find_classifier_target(identity)
        stage_dir = os.path.join(STAGING, "classifiers", identity)
        copies += stage_pngs(stage_dir, identity, items)
        note = f"{len(items)} of {before} confirmed (diversity-sampled, dedup'd)"
        if target:
            plan.append(f"- **{identity}** ({kind}): {note} → `{target}/`")
            install.append(f'echo ">> {identity}: {len(items)} images"')
            install.append(f'cp -v "{stage_dir}"/*.png "{target}/"')
        else:
            plan.append(f"- **{identity}** ({kind}): {note} → ⚠️ no dataset dir found; "
                        f"staged in `{stage_dir}`.")
        install.append("")

    # --- classifiers: confirmed negatives -> dataset/none (hard negatives) ---
    plan.append("\n## Classifiers — hard negatives → none class\n")
    neg_any = False
    for (kind, identity), items in sorted(yes.items()):
        if kind not in CLASSIFIER_KINDS or items[0].get("role") != "negative":
            continue
        neg_any = True
        items = diverse_sample(items, args.cap, args.dedup)
        stage_dir = os.path.join(STAGING, "none", kind)
        copies += stage_pngs(stage_dir, "none", items)
        targets = sorted(none_dirs.get(kind, []))
        plan.append(f"- **{identity}** → none for the {kind} model(s): {len(items)} → "
                    + (", ".join(f"`{t}/`" for t in targets) or "⚠️ no none dir found"))
        install.append(f'echo ">> {kind} none: {len(items)} hard negatives"')
        for t in targets:
            install.append(f'cp -v "{stage_dir}"/*.png "{t}/"')
        install.append("")
    if not neg_any:
        plan.append("- (no confirmed negatives yet — review the 'Other dog/vehicle' piles)")

    # --- faces: rejects become a removal list (yes = already in place) ---
    plan.append("\n## Faces (cleaning: remove mislabeled library images)\n")
    for (kind, identity), items in sorted(no.items()):
        if kind != "person":
            continue
        plan.append(f"- **{identity}**: {len(items)} rejected face(s) to remove "
                    f"from `{FRIGATE_CLIPS}/faces/{identity}/`")
        install.append(f'echo ">> remove {len(items)} rejected faces for {identity}"')
        for c in items:
            install.append(f'rm -v "{c["img"]}"')
            removals += 1
        install.append("")
    for (kind, identity), items in sorted(yes.items()):
        if kind == "person":
            plan.append(f"- **{identity}**: {len(items)} confirmed face(s) — "
                        f"already in library, no action.")

    if not any(k[0] == "person" for k in list(no) + list(yes)):
        plan.append("- (no face verdicts yet)")

    # retire redundant/dead classifier models as part of the same sudo run
    for m in RETIRE_MODELS:
        install.append(retire_classifier_sh(m))

    # post-install hint: bump Frigate to retrain
    install += ["echo", 'echo "Done. Restart/reload Frigate to retrain on the new dataset:"',
                'echo "  sudo docker restart frigate"', ""]

    os.makedirs(PROMOTE, exist_ok=True)
    with open(os.path.join(PROMOTE, "plan.md"), "w") as f:
        f.write("\n".join(plan) + "\n")
    script = os.path.join(PROMOTE, "install_to_frigate.sh")
    with open(script, "w") as f:
        f.write("\n".join(install) + "\n")
    os.chmod(script, 0o755)

    print(f"staged {copies} classifier images, {removals} face removals queued")
    print(f"  plan    -> {os.path.join(PROMOTE, 'plan.md')}")
    print(f"  staging -> {STAGING}/")
    print(f"  install -> {script}   (review, then: sudo {script})")
    if not verdicts:
        print("\n  NOTE: no verdicts yet — go review some in the app first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
