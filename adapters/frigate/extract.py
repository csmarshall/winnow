#!/usr/bin/env python3
"""Extract Frigate tracked-object snapshots into a training workspace.

Reads Frigate's live SQLite DB read-only, copies the snapshot images for the
requested labels out of the clips dir into snapshots/raw/<label>/, and writes a
manifest (JSONL + CSV) with the per-event metadata we'll need for triage:
existing sub_label, score, camera, bounding box, timestamp.

Originals in /opt/nvr/frigate are never touched (copy, not move). Idempotent:
re-running skips files already present.

For each event we grab, in preference order:
  1. <camera>-<id>-clean.webp   (no timestamp/box overlay — best for training)
  2. <camera>-<id>.jpg          (overlay version — fallback)
both are copied when present; the manifest records which exist.

Usage:
  python3 src/extract.py --labels dog car            # the unlabeled hard cases
  python3 src/extract.py --labels person dog car     # everything
  python3 src/extract.py --labels dog --limit 20     # quick sample
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import sys

DB = "/opt/sw/frigate/db/frigate.db"
CLIPS = "/opt/nvr/frigate/clips"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(HERE, "snapshots", "raw")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("extract")


def iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).isoformat() if ts else None


def fetch(labels: list[str], limit: int | None) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    q = (
        "SELECT id, camera, label, sub_label, score, top_score, start_time, "
        "end_time, area, ratio, zones, false_positive, data "
        "FROM event WHERE has_snapshot=1 AND label IN (%s) "
        "ORDER BY label, start_time"
        % ",".join("?" * len(labels))
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = []
    for r in con.execute(q, labels):
        d = dict(r)
        # The normalized [x,y,w,h] box and region live in the data JSON column,
        # not the (null) top-level box column.
        data = json.loads(d.pop("data")) if d.get("data") else {}
        d["box_norm"] = data.get("box")        # [x, y, w, h] as fractions
        d["region_norm"] = data.get("region")
        d["data_score"] = data.get("score")
        d["sub_label_score"] = data.get("sub_label_score")
        rows.append(d)
    con.close()
    return rows


def copy_assets(ev: dict) -> dict:
    """Copy the clean webp and/or overlay jpg for one event. Returns file info."""
    base = f"{ev['camera']}-{ev['id']}"
    dest_dir = os.path.join(RAW, ev["label"])
    os.makedirs(dest_dir, exist_ok=True)
    found = {}
    for kind, fname in (("clean_webp", f"{base}-clean.webp"), ("jpg", f"{base}.jpg")):
        src = os.path.join(CLIPS, fname)
        if os.path.exists(src):
            dst = os.path.join(dest_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            found[kind] = fname
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", nargs="+", required=True,
                    help="object labels to extract, e.g. dog car person")
    ap.add_argument("--limit", type=int, default=None, help="cap rows (sampling)")
    args = ap.parse_args()

    if not os.path.isdir(CLIPS):
        log.error("clips dir not found: %s", CLIPS)
        return 2

    rows = fetch(args.labels, args.limit)
    log.info("matched %d events for labels=%s", len(rows), args.labels)

    manifest, stats = [], {}
    missing = 0
    for ev in rows:
        files = copy_assets(ev)
        if not files:
            missing += 1
        rec = {
            "id": ev["id"],
            "camera": ev["camera"],
            "label": ev["label"],
            "sub_label": ev["sub_label"],
            "score": ev["score"],
            "top_score": ev["top_score"],
            "start_time": iso(ev["start_time"]),
            "box_norm": ev["box_norm"],
            "region_norm": ev["region_norm"],
            "area": ev["area"],
            "ratio": ev["ratio"],
            "zones": ev["zones"],
            "false_positive": ev["false_positive"],
            "files": files,
        }
        manifest.append(rec)
        s = stats.setdefault(ev["label"], {"events": 0, "with_image": 0, "sub_labeled": 0})
        s["events"] += 1
        s["with_image"] += 1 if files else 0
        s["sub_labeled"] += 1 if ev["sub_label"] else 0

    # Write manifest as JSONL (full) and CSV (quick eyeballing).
    os.makedirs(RAW, exist_ok=True)
    jsonl = os.path.join(HERE, "snapshots", "manifest.jsonl")
    with open(jsonl, "w") as f:
        for rec in manifest:
            f.write(json.dumps(rec) + "\n")
    csvp = os.path.join(HERE, "snapshots", "manifest.csv")
    with open(csvp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "camera", "label", "sub_label", "score", "start_time",
                    "clean_webp", "jpg"])
        for r in manifest:
            w.writerow([r["id"], r["camera"], r["label"], r["sub_label"],
                        r["score"], r["start_time"],
                        r["files"].get("clean_webp", ""), r["files"].get("jpg", "")])

    log.info("wrote %s and %s", jsonl, csvp)
    if missing:
        log.warning("%d events had no image file on disk (retention/cleanup)", missing)
    print("\n=== extraction summary ===")
    for lab, s in sorted(stats.items()):
        print(f"  {lab:8} events={s['events']:5d}  images_copied={s['with_image']:5d}  "
              f"already_sub_labeled={s['sub_labeled']}")
    print(f"\n  raw images -> {RAW}/<label>/")
    print(f"  manifest   -> {jsonl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
