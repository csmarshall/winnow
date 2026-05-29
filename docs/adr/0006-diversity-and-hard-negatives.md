# ADR-0006: Diversity over volume; rejections become hard negatives

- **Status:** Accepted — hard-negative routing + within-event keep-set **live**;
  cross-event per-identity sampling **committed, in progress** (task #8)
- **Date:** 2026-05-22

## Context
Our first promote copied *all* confirmed images — e.g. 167 near-identical frames of
one parked DeLorean — straight into the dataset. That is the classic dataset-curation
anti-pattern, and it fails for reasons rooted in **ML fundamentals** that hold for *any*
classifier, not just Frigate's:

- **Near-duplicates are not data.** 167 frames of one sighting are ~one independent
  example. Training on them **overfits** the model to that instance; worse, because the
  duplicates leak across any train/validation split, accuracy metrics look *great* while
  real-world generalization quietly degrades — you measure your way into a false win.
- **Class imbalance biases the model.** If one identity contributes thousands of crops
  and another forty, the model minimizes loss by leaning on the majority class regardless
  of the actual features. (Hence the rule of thumb: keep the largest class within ~3× the
  smallest.)
- **Generalization comes from variety, not count.** Examples spread across time of day,
  lighting, weather, distance, and pose are what produce features that survive new
  conditions — the whole point of supervised training.

Frigate says the same in its own words (*"diversity matters far more than volume;
selecting dozens of nearly identical images is one of the fastest ways to degrade model
performance"*), which is reassuring but not the reason — the reason is the above, and it
would apply to any object/face training pipeline.

Separately, rejected images are signal we were discarding — but a naive "✗ → none" is
wrong: a ✗ on "is this Scooby?" is often Scrappy, not "neither dog," and labeling Scrappy
as `none` would poison the model.

## Decision
- **Curate for diversity, capped per identity — never bulk-copy.** Two layers of the
  same principle:
  - *Within an event* (**live**, ADR-0015): one review card per event; keep a small,
    timeline-spread set and prune the near-duplicate rest from the train pool.
  - *Across events, per identity* (**committed; in progress**, task #8): drop cross-event
    near-duplicates (average-hash Hamming distance) and, once over a per-identity cap,
    spread picks across cameras and hour-of-day; warn when the largest class exceeds ~3×
    the smallest.
- **Hard negatives done safely (live):** only the classifier's confidently-**"other"**
  bucket (human-confirmed "not one of ours") feeds the `none` class — never raw
  per-identity rejections. Faces get **no** negative class (an unrecognized face is an
  overfitting signal, not a training negative — ADR-0014).

## Status / implementation
The hard-negative routing and the **within-event** keep-set are live. The **cross-event,
per-identity** diversity sampling is **decided but not yet wired into the daemon commit
path** — the logic exists in the legacy `promote.py` (`diverse_sample`) but isn't called;
porting it into `commit.plan_actions()` is task #8 / [ROADMAP](../ROADMAP.md). The
*decision* stands on the fundamentals above; only that layer's implementation is pending.
(This ADR is **not** superseded — the position is correct; the code is catching up.)

## Consequences
- Smaller, more varied training sets that generalize better and retrain cheaper.
- Candidates carry a `role` (positive|negative) so commit routes them correctly
  (`dataset/<identity>` vs `dataset/none`).
- The "other" review pile may be empty until neighbor dogs / passing cars appear;
  the plumbing waits for them.

<sub>Frigate's own guidance, which this follows:
[object classification](https://docs.frigate.video/configuration/custom_classification/object_classification)
— *"gather balanced examples across times of day, weather, and distances"*, *"keep
classes visually distinct"*, *"any images not assigned to a specific class will
automatically be assigned to `none`"*;
[face recognition](https://docs.frigate.video/configuration/face_recognition) — *"different
poses, lighting, and expressions"* (and no face negatives — see ADR-0014).</sub>
