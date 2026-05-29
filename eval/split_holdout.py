#!/usr/bin/env python3
"""Cluster-aware train/holdout split for the everything-vs-reduced A/B (task #5).

A naive random split LEAKS: near-identical frames of one event straddle train and
holdout, so the bloated "everything" model effectively memorizes the test set and
scores unfairly high. This clusters crops by average-hash (near-dups grouped) and
assigns WHOLE clusters to either train or holdout — so no near-duplicate spans the
split, and the comparison is honest.

Writes:  <out>/train/<category>/...   and   <out>/holdout/<category>/...
Then:    everything model trains on <out>/train (full, near-dups and all)
         reduced model trains on diversity_sample(<out>/train)
         both are scored on <out>/holdout  (unseen, leakage-free)
READ-ONLY on the source. EVAL TOOLING (not the live path).

Usage:
  python3 eval/split_holdout.py --src <model>/dataset --out /tmp/split \
      --holdout-frac 0.2 --dedup 4 --apply
"""
from __future__ import annotations

import argparse
import os
import random
import shutil

from PIL import Image


def ahash(path: str) -> int:
    im = Image.open(path).convert("L").resize((8, 8))
    px = list(im.getdata())
    avg = sum(px) / len(px)
    return sum((1 << i) for i, p in enumerate(px) if p > avg)


def cluster(files: list[str], dedup: int) -> list[list[str]]:
    """Greedy near-duplicate clustering by avg-hash Hamming distance <= dedup."""
    reps: list[int] = []        # representative hash per cluster
    clusters: list[list[str]] = []
    for f in sorted(files):
        try:
            h = ahash(f)
        except Exception:
            clusters.append([f])      # unreadable -> its own cluster
            continue
        placed = False
        for i, r in enumerate(reps):
            if bin(h ^ r).count("1") <= dedup:
                clusters[i].append(f)
                placed = True
                break
        if not placed:
            reps.append(h)
            clusters.append([f])
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="source dataset dir (category subdirs)")
    ap.add_argument("--out", required=True, help="output dir; gets train/ and holdout/")
    ap.add_argument("--holdout-frac", type=float, default=0.2, help="fraction of crops held out")
    ap.add_argument("--dedup", type=int, default=4, help="avg-hash Hamming distance for clustering")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--apply", action="store_true", help="copy files (default: report only)")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    cats = sorted(d for d in os.listdir(args.src) if os.path.isdir(os.path.join(args.src, d)))
    print(f"{'CATEGORY':18}{'crops':>7}{'clusters':>10}{'train':>8}{'holdout':>9}")
    tb = th = 0
    for cat in cats:
        cdir = os.path.join(args.src, cat)
        files = [os.path.join(cdir, f) for f in os.listdir(cdir)
                 if not f.startswith(".") and os.path.isfile(os.path.join(cdir, f))]
        clusters = cluster(files, args.dedup)
        rng.shuffle(clusters)
        # assign whole clusters to holdout until ~frac of crops, rest to train
        target = args.holdout_frac * len(files)
        hold: list[str] = []
        train: list[str] = []
        for cl in clusters:
            (hold if len(hold) < target else train).extend(cl)
        tb += len(train)
        th += len(hold)
        print(f"{cat:18}{len(files):>7}{len(clusters):>10}{len(train):>8}{len(hold):>9}")
        if args.apply:
            for split, group in (("train", train), ("holdout", hold)):
                od = os.path.join(args.out, split, cat)
                os.makedirs(od, exist_ok=True)
                for f in group:
                    shutil.copy2(f, os.path.join(od, os.path.basename(f)))
    print(f"{'TOTAL':18}{tb+th:>7}{'':>10}{tb:>8}{th:>9}")
    if args.apply:
        print(f"\nwrote {args.out}/train and {args.out}/holdout (clusters never straddle the split)")
    else:
        print("\n(report only — re-run with --apply to write the split)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
