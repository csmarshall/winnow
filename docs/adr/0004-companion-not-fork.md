# ADR-0004: Ship as a Frigate-native companion, not a fork or core PR

- **Status:** Accepted
- **Date:** 2026-05-22

## Context
Winnow's only purpose is improving a Frigate install, which raised: should this
be built *into* Frigate (fork → PR)? Findings:
- Frigate's bulk-assign/multi-select requests are **closed as not planned**
  (#21508, #20398) — maintainers don't want this UX in core.
- The core value (local VLM pre-sort) is a **heavy, optional dependency** that
  doesn't belong in a lean NVR's core.
- Frigate's classification API is **public and sufficient** for an external tool:
  `generate_examples` populates `train/`; `GET .../train` lists; `categorize`
  moves train→dataset; `POST .../train` retrains; `DELETE` removes a model; the
  faces endpoints manage the library.
- The API has **no image-upload** — `categorize` only files images Frigate
  already collected. So injecting our own crops would mean file-copy hacks;
  sourcing from Frigate's own `train/` pool is the sanctioned path.

## Decision
Build Winnow as a **network-connected companion** (runs alongside or on another
box) that drives Frigate's existing API. Do **not** fork Frigate or pursue a core
PR. Keep the VLM pre-sort optional so the review layer could, if ever wanted,
stand alone. Share as a community tool (Show-and-Tell), not a feature request.

## Consequences
- Fast iteration in a small codebase (the lab that fixed cars/dogs quickly).
- Respects Frigate's bookkeeping and survives upgrades (commits via the API).
- We adopt Frigate's data model: candidates come from the `train/` pool, not our
  own extraction — so a one-time L1 file-copy "bank" preserves work done on the
  old crop pipeline before cutting over.
- Re-evaluate upstreaming the plain bulk-reviewer only if maintainers signal
  interest (the API-stability question in the Show-and-Tell).
