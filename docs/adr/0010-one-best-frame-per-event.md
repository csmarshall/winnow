# ADR-0010: One best frame per event (intra-event dedup)

- **Status:** Accepted
- **Date:** 2026-05-27

## Context
Frigate emits many near-identical crops during a *single* tracked-object event
(a dog in frame for 8 seconds → a dozen crops, all the same dog). Presenting all
of them is tedious and adds no training signal. This is distinct from
cross-event diversity (ADR-0006), which dedups *across* events at commit time via
average-hash; here we collapse the *within-event* burst at queue-build time.

There are also non-event files in the train pools — seed/sample images
(`example_*.jpg`) that have no backing Frigate event, so they have no full frame,
clip, or box and can't be reviewed in context.

## Decision
At queue-build time, surface **one card per event** (event = the
`<timestamp>-<id>` filename prefix). Pick the **best frame**: the highest
classifier score (the last field of the train filename) in manual mode —
mirroring AI mode's highest-confidence pick. Ties (e.g. unclassified cars at
`0.0`) break deterministically by filename. **Seed/sample files** that aren't
real events are filtered out entirely. A `WINNOW_DEDUP=0` escape hatch surfaces
every frame for a shakedown run.

## Consequences
- ~3× fewer dog cards (73 real crops → 26 events); the reviewer sees the clearest
  shot per event, not a pile of near-duplicates.
- The clip in the lightbox still exposes every frame for context.
- Cross-event diversity remains ADR-0006's responsibility (commit time).
- Best-frame needs a quality signal; the filename score works for classified
  pools and falls back to deterministic-first when absent.
