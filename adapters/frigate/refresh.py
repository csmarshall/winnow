#!/usr/bin/env python3
"""One-command incremental refresh of the Winnow review queue (Frigate adapter).

The recurring-cadence entrypoint: run this every few days/weeks, then go swipe.
Each step is incremental/resumable, so a refresh only does NEW work:
  extract        copies any new event snapshots (skips ones already pulled)
  classify       classifies only events not already in results_<label>.jsonl
  build_candidates  rebuilds the queue; the app skips anything already decided

Finishes by printing how many NEW candidates await review, per identity, and the
review URL. Nothing to retrain or promote here — that's a separate, deliberate
step once you've reviewed.

Usage:  python3 src/refresh.py
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "src")
PY = sys.executable
LAN = os.environ.get("WINNOW_HOST", "127.0.0.1")
PORT = os.environ.get("REVIEW_PORT", "8077")


def step(title, cmd):
    print(f"\n\033[1m== {title} ==\033[0m")
    subprocess.run(cmd, cwd=HERE, check=True)


def load_jsonl(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def main() -> int:
    step("extract new snapshots", [PY, f"{SRC}/extract.py", "--labels", "person", "dog", "car"])
    for label in ("dog", "car"):
        step(f"classify {label} (incremental)", [PY, f"{SRC}/classify.py", "--label", label])
    step("rebuild review queue", [PY, f"{SRC}/build_candidates.py"])

    # Pending = candidates without a verdict.
    verdicts = {}
    for v in load_jsonl(os.path.join(HERE, "review", "verdicts.jsonl")):
        if v.get("verdict") == "__undo__":
            verdicts.pop(v["cid"], None)
        else:
            verdicts[v["cid"]] = v["verdict"]
    cands = load_jsonl(os.path.join(HERE, "review", "candidates.jsonl"))
    pending = [c for c in cands if c["cid"] not in verdicts]
    by = collections.Counter((c["kind"], c["identity"]) for c in pending)

    print("\n\033[1m== ready to review ==\033[0m")
    if not pending:
        print("  nothing new — you're all caught up.")
    else:
        for (kind, ident), n in sorted(by.items()):
            print(f"  {kind:7} {ident:24} {n} to review")
        print(f"\n  {len(pending)} total new — swipe them at  http://{LAN}:{PORT}")
        print("  (start the app if needed:  python3 src/review_app.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
