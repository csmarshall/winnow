#!/usr/bin/env python3
"""Aggressively trim the Frigate face library to a capped, diverse subset per person.

The library accumulates noise (tiny / blurry / near-duplicate crops) under
permissive `min_area` settings, which over-trains the embedding cluster and
hurts recognition (ADR-0006 — diversity over volume). After raising `min_area`
to filter new captures, this tool cleans the existing pile in one shot:

For each person with > --under-cap images, keep up to --cap that are:
  1. LARGER (image area in pixels — bigger crops embed better), then
  2. DIVERSE (avg-hash hamming distance — drop near-duplicates).
People at or under --under-cap (the sparse, hard-to-capture ones) are LEFT
ALONE — don't punish them for being under-represented.

Files NOT in the keep set are deleted via /api/faces/<name>/delete.
REVERSIBLE: the baseline backup (backup_classification_datasets.sh) snapshots
the face library; restore by un-tarring it. DRY-RUN by default.

USAGE:
  ./eval/.venv/bin/python eval/trim_face_library.py                     # dry run
  ./eval/.venv/bin/python eval/trim_face_library.py --apply             # delete
  ./eval/.venv/bin/python eval/trim_face_library.py --cap 20 --apply
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "adapters", "frigate"))
from frigate_client import FrigateClient   # noqa: E402

_TS = re.compile(r"(\d{9,}\.\d+)")


def ahash_and_area(b: bytes) -> tuple[int, int]:
    """Return (64-bit avg-hash, image area in pixels) for the WebP/JPG bytes."""
    im = Image.open(io.BytesIO(b)).convert("RGB")
    w, h = im.size
    gray = im.convert("L").resize((8, 8))
    px = list(gray.getdata())
    avg = sum(px) / len(px)
    return sum((1 << i) for i, p in enumerate(px) if p > avg), w * h


def pick_keep(items: list[tuple[str, int, int]], cap: int, dedup: int) -> list[str]:
    """items = [(filename, hash, area)]. Returns the kept filenames, prioritizing
    LARGER images and dropping near-duplicates of already-kept ones (greedy by
    descending area). Stops at cap."""
    items_sorted = sorted(items, key=lambda t: (-t[2], t[0]))   # bigger first, ties by name
    kept_hashes: list[int] = []
    kept: list[str] = []
    for name, h, _area in items_sorted:
        if any(bin(h ^ k).count("1") <= dedup for k in kept_hashes):
            continue                                            # near-dup of one we kept
        kept_hashes.append(h)
        kept.append(name)
        if len(kept) >= cap:
            break
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cap", type=int, default=15,
                    help="target images per OVER-represented person (default 15)")
    ap.add_argument("--under-cap", type=int, default=30,
                    help="leave alone if library is at or below this (default 30)")
    ap.add_argument("--dedup", type=int, default=4,
                    help="avg-hash hamming distance for near-dup drop (default 4)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete via /api/faces/<name>/delete (default: dry-run)")
    args = ap.parse_args()

    c = FrigateClient()
    c.login()
    faces = c.list_faces() or {}

    plan: list[tuple[str, int, list[str], list[str]]] = []
    for name in sorted(faces.keys()):
        files = faces[name]
        if name == "train" or not isinstance(files, list):
            continue
        if len(files) <= args.under_cap:
            print(f"  SKIP {name:14} {len(files):>4} files (under-represented, leaving alone)")
            continue
        print(f"  scanning {name:14} {len(files):>4} files (fetching for hash/size)...",
              flush=True)
        items: list[tuple[str, int, int]] = []
        for f in sorted(files):
            try:
                blob = c.fetch_media(f"faces/{name}/{f}")
                h, area = ahash_and_area(blob)
                items.append((f, h, area))
            except Exception as e:
                print(f"    ! fetch {f}: {e}")
        keep = pick_keep(items, args.cap, args.dedup)
        delete = [f for f, _, _ in items if f not in keep]
        plan.append((name, len(items), keep, delete))

    print(f"\n{'PERSON':16}{'before':>8}{'keep':>8}{'delete':>8}")
    for name, total, keep, delete in plan:
        print(f"  {name:14}{total:>8}{len(keep):>8}{len(delete):>8}")

    if not args.apply:
        print("\n(dry run — re-run with --apply to delete)")
        return 0

    print("\napplying...")
    total_deleted = 0
    for name, _total, _keep, delete in plan:
        if not delete:
            continue
        try:
            c.delete_faces(name, delete)
            total_deleted += len(delete)
            print(f"  deleted {len(delete)} from {name}")
        except Exception as e:
            print(f"  ! delete failed for {name}: {e}")
    print(f"\ndeleted {total_deleted} face crops total.")
    print("\nNext: restart Frigate so it reloads the (now smaller, cleaner) library:")
    print("  sudo docker compose -f /opt/sw/docker-compose/frigate/compose.yaml restart frigate")
    print("\nThen in Winnow: 'Check for new' on the home page to refresh the library pools.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
