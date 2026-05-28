# ADR-0006: Diversity over volume; rejections become hard negatives

- **Status:** Accepted
- **Date:** 2026-05-22

## Context
Frigate's own training guidance: *"diversity matters far more than volume;
bulk-selecting similar images degrades the model."* Our first promote copied
*all* confirmed images (e.g. 167 near-identical DeLoreans), which is exactly the
anti-pattern. Separately, rejected images are signal we were discarding — but a
naive "✗ → none" is wrong: a ✗ on "is this Scooby?" is often Scrappy, not "neither
dog," and labeling Scrappy as `none` would poison the model.

## Decision
- **Diversity sampling on commit:** per identity, drop near-duplicates
  (average-hash Hamming distance) and, if still over a cap, spread picks across
  cameras and hour-of-day. Train on a varied subset, not a redundant pile.
- **Hard negatives done safely:** only the classifier's confidently-**"other"**
  bucket (human-confirmed "not one of ours") feeds the `none` class — never raw
  per-identity rejections.

## Consequences
- Smaller, more varied training sets that produce better models per Frigate's
  guidance; cheaper retrains.
- Candidates carry a `role` (positive|negative) so commit routes them correctly
  (`dataset/<identity>` vs `dataset/none`).
- The "other" review pile may be empty until neighbor dogs / passing cars appear;
  the plumbing waits for them.
