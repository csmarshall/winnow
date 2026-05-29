# ADR-0016: Library curation — review what Frigate already committed

- **Status:** Accepted
- **Date:** 2026-05-29

## Context
Frigate has **two** auto-commit confidence gates for face recognition:
`unknown_score` (default 0.8) and `recognition_threshold` (default 0.9). Below
`unknown_score`, a face is "unknown" and lands in Winnow's train pool as
"Needs ID". Between the two, it's a guess and lands in the train pool as
"Is this `<Name>`?". **Above `recognition_threshold`, Frigate auto-commits**:
the crop goes straight into `/clips/faces/<Name>/`, the sub_label is set on the
event, and **the crop never enters the train pool**. The same shape exists for
object classification: high-confidence categorizations land directly in the
dataset.

So when Frigate confidently mis-matches one person to another (e.g. Luigi →
Mario in our case — confirmed observation), Winnow's review queue stays at "0
unmatched," but Mario's library quietly accumulates a Luigi crop that
**actively poisons future recognitions**: it pulls Mario's cluster toward
Luigi-like embeddings, making the *next* Luigi detection even more likely to
get mis-matched. A self-reinforcing imbalance loop. The same can happen for
classifier datasets (a DeLorean crop categorized as Batmobile, etc).

The Frigate UI lets you delete individual library entries one at a time, but
Winnow's whole pitch is "easier curation than that" — and the swipe loop is
exactly the right shape for sweeping a person's existing library.

## Decision
Surface a second **bucket** of pools — "library cleanup" — on Winnow's home
page, distinct from the daily train-pool review. Same identity (e.g. Charles)
can have both a *review* row (today's pending guesses) and a *library* row
(everything Frigate has previously committed to Charles). Same swipe UI, same
Yes / Reassign / Reject vocabulary; different data sources and APIs.

**Verdicts:**
- **Yes** = "still correctly Charles" → no API call; record the verdict so the
  item doesn't reappear (the library pool *shrinks* as you confirm).
- **Reassign** (e.g. *"this is actually Luigi"*) → API moves the crop to the
  correct person/class.
  - Face library: adaptive — Frigate's dev branch has a one-shot
    `POST /faces/{name}/reclassify`; v0.17 doesn't, so we fall back to
    `POST /faces/<new>/register` (upload bytes) + `POST /faces/<old>/delete`
    (same end result). Same caller signature either way — the client probes once
    and remembers (`FrigateClient.face_reclassify`).
  - Classifier dataset: `POST /classification/<model>/dataset/<cat>/reclassify`
    (one-shot, available in v0.17). Triggers a model retrain at end of commit.
- **Reject** = "delete this entirely" → `POST /faces/<name>/delete` for faces;
  `POST /classification/<model>/dataset/<cat>/delete` for dataset images.

**Retrain control:** the existing commit already retrains classifier models
after categorize/reclassify/delete. For bulk library cleanup, dozens of small
corrections shouldn't kick off a retrain after every batch — so the commit
dialog now has a "without retrain" escape hatch. Default stays ON (consistent
with daily review); user opts out for cleanup sessions.

**Enabled by default**, with `WINNOW_LIBRARY_REVIEW=0` to hide if it's noisy.
The cleanup section is auto-hidden when there's nothing in it (clean install).

## Consequences
- The wrong-person self-reinforcing loop is reviewable + closable from inside
  Winnow. The reviewer can browse Mario's library and reassign poisoned crops
  back to Luigi in one swipe, without leaving the app.
- Library reassign is the most valuable training signal: the same crop now
  serves as a hard example for the right person *and* removes a confusing
  example from the wrong person. Pairs naturally with ADR-0006 (diversity over
  volume) and Tenet 5 (capture signal; never silently discard it).
- Two-step face reassign on v0.17 is slightly less efficient than the one-shot
  endpoint coming in v0.18; the adaptive client switches transparently when
  detected. Tracked as a follow-up to simplify the call site.
- The `noop` path in commit must explicitly handle library "yes" so it doesn't
  fall through to the classifier categorize branch (which assumes a
  `training_file`). The plan_actions return is now a `namedtuple` so future
  buckets don't break callers via tuple-unpack.

<sub>Tenets 3 (companion-not-fork), 5 (capture signal), 6 (effortless loop).
Frigate doc that justifies the cleanup workflow: the face_recognition page
recommends "remove most of the unclear or low-quality images" — but Frigate's
own UI does that one file at a time. Winnow makes it a swipe pass.</sub>
