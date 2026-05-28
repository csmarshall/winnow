# ADR-0011: On-the-fly reassignment and new-subtype creation

- **Status:** Accepted
- **Date:** 2026-05-27

## Context
Reviewers hit images the binary "Is this X?" doesn't fit — it's actually a
*different* known subtype, or a *brand-new* one. ADR-0008 flagged this as future
work. Building it required understanding how Frigate models identities, which
turns out to be **three distinct patterns**:

1. **Multi-class classification model** — one model, N categories (the `Scooby`
   model → `Scooby` / `Scrappy` / `none`). Add a subtype by categorizing a training
   file into a new category name; Frigate creates it. **Easy, pure-API.**
2. **Per-identity binary classification models** — each identity is its *own*
   model (`Batmobile`, `DeLorean`; and the person-classifiers
   `Peach`/`Zelda`/`Luigi`/`Mario`), classifying `<thing>` vs `none`. A new
   identity here means **creating a new model**, which lives in Frigate's config
   file — not a per-image API call.
3. **Face recognition (`/faces`)** — the people path Winnow actually reviews; a
   new person is just training a face under a new name, created on the fly.

## Decision
Offer a **"?" reassign** on the **"easy" types only** — multi-class models (dogs)
and faces. A light-pink button (or `r`) opens a **case-insensitive typeahead**
over the known subtypes of that type (so "luigi" matches "Luigi" — no duplicate)
with a **"Create new"** option when nothing matches. It records an
`assign:<target>` verdict (reusing `/api/verdict`); commit routes it to
`categorize(model, target)` for dogs or `classify_face_train(target)` for faces —
a new target name is **created by Frigate at commit**. A reassignment counts as a
claim, so the binary-sweep exclusion (ADR-0008) removes the image from sibling
pools. Targets are auto-derived (a dog model's categories; the face library
names). **Cars (pattern 2) are excluded** — a new car needs a new model in config.

## Consequences
- Edge/mistaken images get assigned precisely without leaving the swipe flow; new
  dogs and people can be created mid-review.
- New binary-model identities (a third car) still require a Frigate config change
  first; once added, Winnow can auto-discover the model (follow-up).
- Casing is normalized to an existing subtype's canonical spelling, preventing
  duplicate categories ("luigi" → "Luigi").
- Realizes the future note in ADR-0008.
