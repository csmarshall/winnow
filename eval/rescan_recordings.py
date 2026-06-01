#!/usr/bin/env python3
"""Harvest face-crop CANDIDATES from existing recordings, then funnel each one
through Winnow's human review (task #14, ADR-0016 follow-up). Two-phase split,
deliberately:

  PHASE 1 (this script):  walk recent person events, ask Frigate's recognize
                          to identify each event snapshot using the CURRENT
                          (trimmed/clean) library, and emit ONE pending
                          candidate per confident match into
                          review/rescan_candidates.jsonl.
  PHASE 2 (review_app):   build_candidates.from_rescan reads that file and
                          surfaces one "Is this <recognized>?" swipe card per
                          candidate (bucket="rescan"). On commit, Yes ->
                          face_register to the recognized name; Reassign:Other
                          -> register to Other; No/Reject -> noop (just dropped
                          from queue).

Why two phases: the dry-run on a real library revealed face-recognize can be
wrong in aggregate (a trimmed library that's pose-biased can over-attribute
faces to the most-frontal-trained identity), so auto-register would re-bloat
the library with cross-identity poison. Manual confirmation closes that loop
without losing the value of the cheap server-side scan.

USAGE:
  ./eval/.venv/bin/python eval/rescan_recordings.py                   # 7-day scan
  ./eval/.venv/bin/python eval/rescan_recordings.py --days 14
  ./eval/.venv/bin/python eval/rescan_recordings.py --min-confidence 0.95
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "adapters", "frigate"))
from frigate_client import FrigateClient, FrigateError   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW = os.path.join(HERE, "..", "review")
PROCESSED = os.path.join(REVIEW, "rescanned_events.txt")
CAND_FILE = os.path.join(REVIEW, "rescan_candidates.jsonl")


def load_processed() -> set:
    if not os.path.exists(PROCESSED):
        return set()
    return {ln.strip() for ln in open(PROCESSED) if ln.strip()}


def mark_processed(ids: list[str]) -> None:
    os.makedirs(os.path.dirname(PROCESSED), exist_ok=True)
    with open(PROCESSED, "a") as f:
        for i in ids:
            f.write(i + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    ap.add_argument("--min-confidence", type=float, default=0.85,
                    help="recognize threshold below which we skip (default 0.85)")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max events to consider in the time window (default 2000)")
    args = ap.parse_args()

    c = FrigateClient()
    c.login()

    after = time.time() - args.days * 86400
    events = c.list_events(after=after, labels="person", has_snapshot=True, limit=args.limit)
    print(f"window: last {args.days} days  ->  {len(events)} person events with snapshots")
    processed = load_processed()
    todo = [e for e in events if e["id"] not in processed]
    print(f"already-processed events: {len(events)-len(todo)}    NEW to scan: {len(todo)}")

    if not todo:
        print("nothing new to do.")
        return 0

    todo.sort(key=lambda e: e["start_time"], reverse=True)   # newest first

    os.makedirs(REVIEW, exist_ok=True)
    out = open(CAND_FILE, "a")             # append so re-runs accumulate

    n_recognized = n_low = n_no_face = n_emitted = 0
    just_processed: list = []

    for i, ev in enumerate(todo, 1):
        eid = ev["id"]
        try:
            snap = c.fetch_event_snapshot(eid)
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {eid}: snapshot fetch failed ({e}) — skip")
            continue
        try:
            r = c.face_recognize(snap, filename=f"{eid}.jpg")
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {eid}: recognize failed ({e}) — skip")
            continue

        if not r or not r.get("success"):
            n_no_face += 1
            just_processed.append(eid)      # no face = nothing to harvest, don't reconsider
            continue

        name = r.get("face_name")
        score = float(r.get("score") or 0)
        n_recognized += 1
        if score < args.min_confidence:
            n_low += 1
            just_processed.append(eid)      # low confidence — don't re-ask next run
            continue

        # Emit a pending candidate. The candidate carries everything review/commit
        # need: event id (for snapshot URL), the recognized name (the proposed
        # assignment), Frigate's old sub_label (for context: did the trim change it?),
        # and the camera (for the full-scene fallback chain).
        out.write(json.dumps({
            "event_id": eid, "camera": ev["camera"],
            "start_time": ev["start_time"], "end_time": ev["end_time"],
            "recognized": name, "score": score,
            "old_sub_label": ev.get("sub_label"),
            "scanned_at": time.time(),
        }) + "\n")
        out.flush()
        n_emitted += 1
        just_processed.append(eid)

        if i % 50 == 0:
            print(f"  [{i}/{len(todo)}] recognized={n_recognized}  emitted={n_emitted}")

    out.close()
    mark_processed(just_processed)

    print(f"\n=== summary ===")
    print(f"  scanned                          : {len(todo)}")
    print(f"  recognized                       : {n_recognized}")
    print(f"  no face detected                 : {n_no_face}")
    print(f"  recognized, below {args.min_confidence:.2f}             : {n_low}")
    print(f"  emitted to review queue          : {n_emitted}")
    print(f"\nWrote {n_emitted} pending candidates to {CAND_FILE}.")
    print("Now refresh Winnow (Check for new) — each will appear in the new 'Rescan'")
    print("section. Confirm/reassign/reject; Yes registers it into the recognized")
    print("person's library at full event-snapshot resolution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
