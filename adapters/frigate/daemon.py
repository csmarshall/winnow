#!/usr/bin/env python3
"""Winnow daemon — Frigate adapter (ADR-0007).

The always-ready app: it serves the source-agnostic swipe UI and wires it to a
Frigate-specific refresh that runs on a timer and on demand. A reviewer (e.g.
Zelda) just opens the page, swipes the pools, and clicks "Check for new" when
caught up — no CLI, no API knowledge.

A refresh does, in order:
  1. commit previously-confirmed verdicts back to Frigate (categorize + retrain)
  2. ask Frigate to collect fresh examples, then VLM-pre-sort the train pool
  3. rebuild the review queue

Run:  python3 daemon.py            # binds 0.0.0.0:8077, auto-refresh every 30 min
Env:  WINNOW_REFRESH_SEC (default 1800), plus the FRIGATE_* / OLLAMA_* config.
"""
from __future__ import annotations

import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "winnow"))  # the core package

import review_app                       # noqa: E402  (core)
import build_candidates                 # noqa: E402
import classify                         # noqa: E402
import commit                           # noqa: E402
from frigate_client import FrigateClient   # noqa: E402

log = logging.getLogger("winnow.daemon")
INTERVAL = int(os.environ.get("WINNOW_REFRESH_SEC", "1800"))
MANUAL = os.environ.get("WINNOW_MANUAL", "").lower() in ("1", "true", "yes")
NO_COMMIT = os.environ.get("WINNOW_NO_COMMIT", "").lower() in ("1", "true", "yes")


def refresh() -> str:
    """Refresh hook — bring in fresh candidates. Does NOT commit (ADR-0013):
    verdicts accumulate locally until the reviewer explicitly hits Commit."""
    c = FrigateClient()
    c.login()

    # collect fresh examples + pre-sort each model's train pool
    # (skipped in manual mode — the reviewer sorts the raw pool by hand)
    classified = 0
    if not MANUAL:
        for model, cfg in build_candidates.discover_models(c).items():
            try:
                res = classify.classify_model(model, cfg["kind"], generate=True, client=c)
                classified += res["classified"]
            except Exception as e:
                log.warning("classify %s failed: %s", model, e)

    res = build_candidates.build(client=c, manual=MANUAL)
    mode = "manual" if MANUAL else f"{classified} pre-sorted"
    return f"{res['count']} to review ({mode})"


def commit_changes() -> str:
    """Commit hook — user-triggered. Push confirmed verdicts to Frigate, then
    rebuild so the committed (categorized) train files drop out of the pools and
    only the leftovers + anything new remain. NO_COMMIT -> dry-run (no writes)."""
    c = FrigateClient()
    c.login()
    res = commit.run(apply=not NO_COMMIT, do_train=True, client=c)
    build_candidates.build(client=c, manual=MANUAL)   # rebuild -> pools reflect commit
    if NO_COMMIT:
        return "dry-run (WINNOW_NO_COMMIT=1) — nothing written to Frigate"
    return (f"pushed {res['categorized']} classifications + {res['faces']} faces, "
            f"deleted {res['deleted']}; retrained {', '.join(res['trained']) or 'nothing'}")


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    review_app.COMMIT_FN = commit_changes        # user-triggered commit (ADR-0013)
    review_app.SOURCE_NAME = "Frigate"
    review_app.serve(refresh_fn=refresh, interval=INTERVAL, first_load=True)
