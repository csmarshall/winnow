# ADR-0001: Model-assisted, human-in-the-loop training-data curation

- **Status:** Accepted
- **Date:** 2026-05-22

## Context
Building good training sets for Frigate's classifiers/face recognition means
labeling lots of snapshots. Two naive approaches both fail: hand-labeling one
image at a time is miserable at scale (hundreds of crops), and fully automatic
labeling is wrong often enough that you can't trust the output as "known-good."

## Decision
Split the work by what each party is good at:
- A **local vision model** does first-pass perception/bucketing at scale (cheap,
  on-prem, no per-image cost).
- A **human** does fast yes/no judgment via a Tinder-style swipe UI, surfaced
  **lowest-confidence-first** so attention lands where the model is weakest.
- The model's pre-sort is an **accelerator, never the authority** — nothing is
  "known-good" until a human confirms it.

## Consequences
- Reviewing hundreds of images becomes minutes of swiping, not an afternoon.
- Requires a local VLM (GPU) for the pre-sort — but it stays optional: with it
  off, the tool is still a plain bulk reviewer (keeps it upstream-palatable).
- Creates a flywheel: confirmed data retrains the model → fewer corrections next
  cycle. Enables the recurring "review every few weeks" cadence.
