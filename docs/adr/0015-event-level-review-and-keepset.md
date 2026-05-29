# ADR-0015: Event-level review — one card per event, keep a capped diverse set

- **Status:** Accepted
- **Date:** 2026-05-29
- **Refines:** ADR-0010 (one best frame per event)

## Context
Frigate saves **many near-identical crops per tracked object** — a car parked in view
for a few minutes produced **19 crops** in one real case, and its classifier guessed
the *same car* inconsistently frame-to-frame (DeLorean on most, "Batmobile" on three,
"none" on four). ADR-0010 deduped this for *review* (show one best frame per event),
but only for the **presentation** — the un-reviewed sibling crops stayed in the train
pool. Three failures followed:

1. **It never ends.** Commit a crop → rebuild → the next sibling of the same event
   surfaces. "I review this car and it keeps coming back."
2. **Scattered across pools.** Because each crop was pooled by Frigate's *per-frame*
   guess, one car's frames landed in the DeLorean, Batmobile, *and* none pools — clearing
   one pool never cleared the others.
3. **Overfitting risk.** Categorizing all 19 near-identical frames over-weights one
   sighting — the opposite of Frigate's "diversity over volume, cap similar" guidance.

## Decision
Make the **event** (Frigate's tracked-object id, the `<ts>-<rand>` filename prefix)
the unit of review and of training-data selection.

- **One review card per event.** Group the train pool by event id; present the single
  best (highest-confidence) crop as "Is this `<guess>`?" with the normal swipe cross.
- **Decide once, apply to the whole event.** On commit, categorize a **small, capped,
  timeline-spread keep-set** (`WINNOW_KEEP_PER_EVENT`, default **3**) to the decided
  class, and **delete the remaining sibling crops** from the train pool (new
  `POST /classification/{model}/train/delete`). The keep-set is chosen at *build* time
  and carried on the candidate (`keep_files`/`keep_urls`) so what the reviewer sees in
  the lightbox filmstrip is exactly what gets trained — they can hand-validate the
  frames are the same entity before confirming.
- **Why a *set*, not one.** "One best frame" (ADR-0010) eliminated near-duplicates but
  also threw away useful intra-event variation (arriving / parked / leaving = different
  angles, distance, light). A few *genuinely diverse* frames train better than one;
  near-duplicates still don't. `KEEP_PER_EVENT=1` recovers the strict ADR-0010 behavior.
- The human's decision is authoritative: a frame Frigate mis-guessed (the "Batmobile"
  frames of a DeLorean) is categorized to the human's class or deleted, never left to
  haunt another pool.

## Consequences
- A parked car is **one card, not nineteen**; nothing resurfaces; pools stop being
  polluted by one event's inconsistent guesses.
- Training stays **diverse, not voluminous** — honors Frigate's guidance.
- We now **delete** train-pool crops. This is curtailing input data, so the keep count
  is a **tunable knob** (raise if under-trained, lower if overfitting), and validating
  that pruning doesn't hurt accuracy is a real follow-up (see ROADMAP).
- The existing dataset is untouched — only *new* per-event additions are capped.

## Frigate references (the best practices we're following)
- [Object classification](https://docs.frigate.video/configuration/custom_classification/object_classification):
  *"Use the model's Recent Classification tab to gather balanced examples across times of
  day, weather, and distances"* and *"Keep classes visually distinct to improve accuracy."*
  Our capped, timeline-spread keep-set serves "balanced/diverse, visually distinct" rather
  than dozens of near-identical frames of one sighting.
- The `none` class is Frigate's own hard-negative bucket: *"Any images not assigned to a
  specific class will automatically be assigned to `none`."*

<sub>Tenet 4 (honor the source's best practices) and Tenet 6 (effortless loop). Note
this consciously revises Tenet 4's "one best frame per event" to "a small, capped,
diverse set per event" — same intent (no near-duplicate flooding → no overfitting).</sub>
