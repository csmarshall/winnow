#!/usr/bin/env python3
"""Regenerate a DIVERSE, reduced dataset from a (bloated) Frigate classification
dataset — the offline tool for the A/B experiment (task #5 / #8, ADR-0006/0015).

Per category, it:
  1. drops near-duplicate crops (average-hash Hamming distance <= --dedup), then
  2. if still over --cap, spreads the kept picks across hour-of-day so the training
     set is varied rather than redundant.
Copies the kept crops into <out>/<category>/. READ-ONLY on the source.

NOTE: this is EVAL TOOLING — deliberately NOT wired into the live commit path (that
is task #8). It exists to build the "reduced" dataset for the everything-vs-reduced
A/B, so we measure before changing production (validate-first).

Frigate flattens the event id when it categorizes a crop (`<Category>-<ts>-<rand>.png`),
so on historical data the avg-hash dedup — which catches near-identical frames
regardless of naming — is the workhorse; hour-of-day comes from the <ts>.

Usage:
  python3 eval/diversity_sample.py --src <model>/dataset --out /tmp/reduced        # report
  python3 eval/diversity_sample.py --src <model>/dataset --out /tmp/reduced --apply # write
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import re
import shutil

from PIL import Image

_TS = re.compile(r"(\d{9,}\.\d+)")


def ahash(path: str) -> int:
    """64-bit average hash of an image (near-duplicate detection)."""
    im = Image.open(path).convert("L").resize((8, 8))
    px = list(im.getdata())
    avg = sum(px) / len(px)
    return sum((1 << i) for i, p in enumerate(px) if p > avg)


def hour_of(fname: str) -> str:
    """UTC hour-of-day from the unix ts embedded in a dataset crop filename."""
    m = _TS.search(fname)
    if not m:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(float(m.group(1)), datetime.UTC).strftime("%H")
    except (ValueError, OverflowError):
        return "?"


def sample_category(files: list[str], cap: int | None, dedup: int) -> list[str]:
    """Near-dup drop (avg-hash Hamming <= dedup), then cap with hour-of-day spread."""
    kept, hashes = [], []
    for f in sorted(files):
        try:
            h = ahash(f)
        except Exception:
            kept.append(f)        # unreadable -> keep rather than silently drop
            continue
        if any(bin(h ^ k).count("1") <= dedup for k in hashes):
            continue              # near-duplicate of something already kept
        hashes.append(h)
        kept.append(f)
    if cap is None or len(kept) <= cap:
        return kept
    groups: dict = collections.OrderedDict()
    for f in kept:
        groups.setdefault(hour_of(os.path.basename(f)), []).append(f)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="source dataset dir (has category subdirs)")
    ap.add_argument("--out", required=True, help="output reduced dataset dir")
    ap.add_argument("--cap", type=int, default=80,
                    help="max diverse crops per category (diversity > volume); 0 = no cap")
    ap.add_argument("--dedup", type=int, default=4,
                    help="drop crops within this avg-hash Hamming distance as near-dups")
    ap.add_argument("--apply", action="store_true", help="copy the kept crops (default: report only)")
    args = ap.parse_args()

    cap = None if args.cap == 0 else args.cap
    cats = sorted(d for d in os.listdir(args.src) if os.path.isdir(os.path.join(args.src, d)))
    if not cats:
        print(f"no category subdirs under {args.src}")
        return 2

    print(f"{'CATEGORY':18}{'before':>8}{'after':>8}{'kept %':>8}")
    tot_b = tot_a = 0
    for cat in cats:
        cdir = os.path.join(args.src, cat)
        files = [os.path.join(cdir, f) for f in os.listdir(cdir)
                 if not f.startswith(".") and os.path.isfile(os.path.join(cdir, f))]
        kept = sample_category(files, cap, args.dedup)
        tot_b += len(files)
        tot_a += len(kept)
        pct = 100 * len(kept) / max(1, len(files))
        print(f"{cat:18}{len(files):>8}{len(kept):>8}{pct:>7.0f}%")
        if args.apply:
            od = os.path.join(args.out, cat)
            os.makedirs(od, exist_ok=True)
            for f in kept:
                shutil.copy2(f, os.path.join(od, os.path.basename(f)))
    print(f"{'TOTAL':18}{tot_b:>8}{tot_a:>8}{100*tot_a/max(1,tot_b):>7.0f}%")
    if args.apply:
        print(f"\nwrote reduced dataset -> {args.out}")
    else:
        print("\n(report only — re-run with --apply to write the reduced dataset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
