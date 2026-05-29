# ADR-0014: The verdict model — reassign first-class, "no" is unresolved, reject deletes

- **Status:** Accepted
- **Date:** 2026-05-27

> **Implementation update (ADR-0015):** the dog/car **reshuffle** below was originally
> two verdicts (`no` on the source sibling + `yes` on the target sibling). Now that
> manual mode is **event-level** (one candidate per event, no sibling pools), a reassign
> is a **single `assign:<target>`** verdict and `identities()` does the pool accounting
> (source shows *moved*, target shows *allocated*). The **Unresolved** bucket for face
> "no" is **not yet implemented** — a face "no" currently no-ops (parks in place); it's
> tracked in [ROADMAP](../ROADMAP.md).

## Context
Verdicts began as yes / no / skip, with reassignment bolted on later as a secondary
"?" escape hatch. In use, **"no" is a dead end that loses signal** — most starkly
for faces: "no" (not this person) is a commit no-op, so the face is never assigned
to anyone *and* never removed; it lingers unconfirmed in the source forever and we
learn nothing from the review. Meanwhile the genuinely useful action — saying *who*
something actually is — was the buried one.

Two structural facts shape the fix:
- **Binary-sweep types (dogs/cars):** the same image is a sibling candidate in every
  subtype pool. So a "no" in one pool naturally **defers to the others** (it's
  re-assessed there), and a reassign is just "no here + yes in the target pool."
- **Faces:** there are no sibling pools (one candidate per recognized face), so a
  "no" has nowhere to defer — it just vanishes.

## Decision
Promote **reassign to a first-class action** and give every form of rejection a
meaning that doesn't throw away signal.

**Verdict vocabulary:**
- **yes** — confirm the proposed label → commit to that identity.
- **reassign (`assign:X`)** — *first-class.* "It's actually X" (existing subtype or a
  brand-new one). Capture path when the guess is wrong.
  - *dogs/cars (sibling exists):* **reshuffle** — record **no** on the source sibling
    and **yes** on the target sibling. Both pools' tallies update, the target pool
    pre-fills, and the group-claim drops the image from the remaining pools. (Undo
    reverts both.)
  - *faces (no sibling):* record `assign:X` on the single candidate → commit
    classifies it to X (X created if new).
- **skip** — defer for this session (stays pending).
- **no** — "not this — unresolved."
  - *dogs/cars:* defers to the sibling pools (already the case).
  - *faces:* routes to an **Unresolved** bucket — a revisitable pool you can reassign
    from later — instead of discarding. No identity signal is lost.
- **reject / not-one-of-ours** — *not a tracked identity.* Covers both genuine
  garbage (bad crop, not a face) **and** real-but-untracked people (the one-off
  passerby / FedEx guy you won't name). Commit **deletes** it from the source
  (Frigate's train pool), training no one, so it stops being re-suggested.
  - *Recurring* strangers (FedEx, mailman, a regular neighbor) are better handled by
    **reassign → Create a catch-all identity** (`Delivery` / `Stranger` /
    `Not household`): Frigate then *recognizes* them as that bucket, they stop
    surfacing as unknown, and downstream automations can treat the label as
    non-household. NOTE: per Frigate's docs this is a **positive named identity**,
    *not* a negative — **Frigate has no negative training for faces** (unknowns that
    match a known person indicate overfitting, fixed with more diverse data). So a
    face "reject" only ever **deletes** (Frigate's remove-and-reprocess lever); we
    never try to train a face negative. (The classifier `none` hard-negative of
    ADR-0006 applies to object classification, which *does* support it — not faces.)

**Unidentified faces.** Frigate detects faces it can't name (its "unknown" bucket).
Today these are dropped (`from_faces` skips `unknown`). Under this model they are the
*purest* "who is this?" candidates — Frigate gave up, so a human label is maximum
signal — so they are **surfaced for identification**, not discarded. The
human-rejected-guess pile ("no → unresolved") and Frigate's unknown faces are the
same kind of thing ("a human needs to name this"), so they **merge into one
"Needs ID" pool**, triaged with the same reassign-first-class UI: identify (existing
or new), reject, or defer.

**UI:** reassign is surfaced as a primary action, not a buried "?". For faces the
card reads as *"who is this?"* — confirm the guess, reassign/identify, reject the
garbage, or skip. So every face Frigate detected ends up confirmed, identified,
rejected, or parked — **never silently dropped**.

Per-model-type decision flows (Mermaid): see [`docs/flows.md`](../flows.md).

## Consequences
- Reassign becomes *the* signal-capture action; "no" never silently discards (dogs
  re-assess across sibling pools; faces park in Unresolved); "reject" is the explicit
  cleanup path.
- New machinery: an **Unresolved** pool (faces verdicted "no"), a **reject** verdict,
  a Frigate **delete-train-face** call in commit, and the dog/car **reshuffle**
  (two verdicts per reassign; undo reverts both).
- This supersedes the implicit "face no = noop discard." Classifier hard-negatives
  (ADR-0006, the `none` bucket) are unchanged.
- This ADR plus 0001–0013 are candidates for a **v1 "tenets" consolidation** — a
  cohesive statement of principles before the first real release (see ROADMAP).
