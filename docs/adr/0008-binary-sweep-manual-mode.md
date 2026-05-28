# ADR-0008: Binary-sweep manual mode (no-AI), unified with AI mode

- **Status:** Accepted
- **Date:** 2026-05-26

## Context
We need a mode for a fresh user with no VLM priming — "come in with just the
types and subtypes and sort." The first cut presented one "Unsorted" pile with
N-way choice buttons ("which subtype is this?"). But N-way menus are slower and
cramped on phones, and they hid the user's subtypes. Prior art (Apple/Google
Photos "is this the same person?", Lightroom culling, Prodigy labeling) all
converge on the same finding: **binary accept/reject is faster and more
phone-friendly than an N-way choice**, and Prodigy explicitly decomposes
multi-class problems into sequences of binary questions.

## Decision
Manual mode = the **same per-subtype binary pools as AI mode, just unfiltered**.
Every train image enters EACH subtype's pool as a binary "Is this <subtype>?"
sweep. Candidates for one image share a `group`; a "yes" in any pool removes the
image from the other pools (review app enforces via `_assigned_groups`). Images
never confirmed anywhere are the leftover "unsorted". So the home surfaces each
subtype as a pool, and a reviewer sweeps Scooby (N), then Scrappy (N − Scooby
matches), etc.

Manual candidates are structurally identical to AI-mode positives (role positive,
identity=subtype, model, training_file) — only the VLM confidence/reason and the
pre-filtering differ — so commit and the swipe UI need no special path.

## Consequences
- One consistent interface for both modes; the AI is purely an optional
  pre-filter on which images appear in each pool. Same muscle memory, same cids
  (so verdicts carry across modes).
- Phone-friendly: reuses the yes/no swipe (two thumb zones), scales to any number
  of subtypes without UI changes.
- Cost: with no AI, worst case N swipes per image (one per subtype) — accepted as
  the price of no priming; the pool shrinks each sweep as matches leave.
- The earlier N-way "choices" path is superseded (left in place but unused).
- A reviewer can also **reassign** an image to a different subtype, or create a
  NEW one on the fly — built as the "?" typeahead; see **ADR-0011**.
