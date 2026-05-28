# ADR-0012: Source-agnostic core, Frigate-first reality

- **Status:** Accepted
- **Date:** 2026-05-27

## Context
The docstrings and ADR-0007 describe a "source-agnostic core" with a pluggable
adapter. That's directionally true but easy to oversell, so this records the
honest state:

- **Genuinely agnostic (the plumbing):** the core (`winnow/review_app.py`) imports
  nothing Frigate. It's stdlib-only, serves `candidates.jsonl`, records
  `verdicts.jsonl`, and is driven entirely by an injected `REFRESH_FN` hook. The
  swipe UI, pool aggregation, binary-sweep exclusion, and build-id resume all
  operate on generic candidate dicts. The Frigate adapter (`adapters/frigate/`) is
  cleanly isolated and the core never reaches into it.
- **Frigate-first in practice:** there is exactly **one** adapter, the abstraction
  is **untested against a second source**, and a few Frigate concepts have settled
  into the core's data contract (see leak points below).

## Decision
Keep the core/adapter seam and the Frigate-first framing (ADR-0004 = companion,
not a general tool). **Do not oversell** the agnosticism — document the contract an
adapter must satisfy so a second source is a known quantity rather than a claim.

### The adapter contract (what the Frigate adapter provides today)
A source adapter must:

1. **Write `review/candidates.jsonl`** — one JSON object per candidate. Fields the
   core reads are source-neutral; a few are pass-through for the adapter's own
   commit:
   - **Core-used:** `cid` (stable unique id), `kind` (pool type, e.g.
     person/dog/car), `identity` (subtype/label), `img_url` (thumbnail/crop),
     `full_url` (full scene), `clip_url` (optional), `confidence` (sort key, may be
     null), `group` (binary-sweep exclusivity key), `choices` (optional N-way),
     `box` (optional normalized overlay), plus display-only `reason`/`source`/
     `meta`/`question`.
   - **Adapter pass-through (opaque to the core):** `model`, `training_file`,
     `face_train` — Frigate-specific; the core just carries them.
2. **Supply a `REFRESH_FN`** (`callable() -> summary str`) to `review_app.serve`.
   It does the source-specific fetch → pre-sort → write-candidates, and typically
   commits confirmed verdicts first. The core calls it (timer + on demand), then
   reloads.
3. **Persist decisions back to the source** — read `review/verdicts.jsonl`
   (`{cid, verdict}`, where verdict ∈ yes/no/skip/`assign:<target>`/chosen subtype)
   and apply them however the source requires. (Frigate: categorize / face-classify
   + retrain.)
4. **Optionally write `review/targets.json`** for the "?" reassign picker.

### Leak points (Frigate-isms in the "agnostic" core)
- The candidate schema carries `model`/`training_file`/`face_train`.
- The reassign-targets shape `{dog: {model: [categories]}, person: [names]}` mirrors
  Frigate's *classification-vs-face-recognition* duality, not a neutral model.
- The `kind` taxonomy (`person`/`dog`/`car` in `KIND_ORDER`) is example-driven.

## Consequences
- The claim is now scoped honestly: a clean seam and reusable plumbing, but one
  adapter and an unproven abstraction.
- Adding a second source (a folder of images, another NVR) means: write an adapter
  satisfying the contract above, and generalize the two leak points that touch the
  core — the reassign-targets shape and the `kind` taxonomy. The swipe / verdict /
  queue / resume machinery should work unchanged.
- We deliberately don't refactor for hypothetical sources now (YAGNI); this ADR is
  the breadcrumb for whoever does.
