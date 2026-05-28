#!/usr/bin/env python3
"""Native classify: VLM-pre-sort a Frigate model's train/ pool over the API.

Replaces the old crop-based pipeline (ADR-0004): instead of extracting+cropping
our own snapshots, we let Frigate collect object crops into its train/ pool
(`generate_examples`), pull them via the API, and run the local VLM pre-sort
(vlm.py) on each. Output: results_<model>.jsonl — one row per train image with
the predicted identity/bucket + the observations behind it.

Images are fetched over HTTP (network-reachable), so this works whether Winnow
runs beside Frigate or on another box.

Usage:
  python3 classify.py --model Scooby                 # classify Scooby's whole train pool
  python3 classify.py --model Scooby --limit 10      # smoke test
  python3 classify.py --model Scooby --generate      # ask Frigate to collect fresh examples first
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys

from PIL import Image

import vlm
from frigate_client import FrigateClient, FrigateError

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "..", "..", "review")

# Frigate model name -> scheme kind. (Cars need their own models/train pools;
# add them here once consolidated — see ADR-0004 follow-ups.)
MODELS = {"Scooby": "dog"}

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("classify")


def event_id(filename: str) -> str:
    """Frigate names train files <ts>-<rand>-<ts2>-unknown-<score>.webp."""
    parts = filename.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else filename


def start_time(filename: str):
    m = re.match(r"(\d+\.\d+)", filename)
    return float(m.group(1)) if m else None


def results_path(model: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    return os.path.join(RESULTS_DIR, f"results_{safe}.jsonl")


def classify_model(model, kind=None, conf=0.65, limit=None, generate=False,
                   client=None) -> dict:
    """Classify a model's train/ pool (incremental/resumable). Importable by the
    daemon. Returns a summary dict. Pass a logged-in `client` to reuse it."""
    kind = kind or MODELS.get(model)
    if not kind:
        raise ValueError(f"no kind for model {model!r} — pass kind=")
    scheme = vlm.SCHEMES[kind]
    c = client or FrigateClient()
    if client is None:
        c.login()
    if generate:
        log.info("generate_examples for %s (%s) ...", model, kind)
        c.generate_object_examples(model, kind)

    files = c.list_train(model)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    rp = results_path(model)
    done = ({json.loads(l)["training_file"] for l in open(rp)}
            if os.path.exists(rp) else set())
    todo = [f for f in files if f not in done]
    if limit:
        todo = todo[:limit]
    log.info("%s: pool=%d, classifying %d new", model, len(files), len(todo))

    tally = {}
    with open(rp, "a") as out:
        for i, f in enumerate(todo, 1):
            try:
                img = Image.open(io.BytesIO(c.fetch_train_image(model, f)))
                r = vlm.classify_image(img, scheme, conf)
            except Exception as e:
                log.warning("%s: %s", f, e)
                continue
            rec = {"training_file": f, "model": model, "kind": kind,
                   "event_id": event_id(f), "start_time": start_time(f), **r}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            tally[r["bucket"]] = tally.get(r["bucket"], 0) + 1
            if i % 10 == 0 or i == len(todo):
                log.info("[%d/%d] %s", i, len(todo), dict(sorted(tally.items())))
    return {"model": model, "kind": kind, "pool": len(files),
            "classified": len(todo), "tally": tally, "results_path": rp}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Frigate classification model name")
    ap.add_argument("--kind", choices=list(vlm.SCHEMES),
                    help="scheme kind (default: from MODELS map)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--conf", type=float, default=0.65,
                    help="min confidence to accept an identity; else -> review")
    ap.add_argument("--generate", action="store_true",
                    help="trigger Frigate generate_examples for this model first")
    args = ap.parse_args()
    try:
        res = classify_model(args.model, args.kind, args.conf, args.limit, args.generate)
    except ValueError as e:
        log.error(str(e))
        return 2
    print(f"\n=== {res['model']} ({res['kind']}) buckets ===")
    for b, n in sorted(res["tally"].items()):
        print(f"  {b:16} {n}")
    print(f"\n  results -> {res['results_path']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FrigateError as e:
        print(f"frigate error: {e}")
        raise SystemExit(1)
