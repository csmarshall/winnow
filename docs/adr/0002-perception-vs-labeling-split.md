# ADR-0002: The VLM does perception; code assigns identity

- **Status:** Accepted
- **Date:** 2026-05-22

## Context
First attempts asked the VLM to identify entities directly ("is this the DeLorean?",
"is this Scooby?"). This failed in two distinct ways observed on real
data:
- **Brand/identity hedging:** asked to name a *make/model*, the model refused to
  commit from a security cam ("can't read the badge") and returned `unsure` even
  on an obvious white SUV — 214/283 cars went `unsure`, 0 DeLorean.
- **Confidently wrong identity:** told "brown=Scooby, black=Scrappy", a brindle
  (dark-striped) Scooby read as dark → confidently mislabeled Scrappy (50% of the
  Scrappy bucket was actually Scooby), all daytime, all high-confidence.

The root cause both times: the model was asked to make the *labeling* decision,
where it carries biases and refuses ambiguity poorly.

## Decision
Constrain the model to **neutral perception** — report observable attributes
(dominant color, build/proportions, harness present, coat pattern) — and do the
**identity mapping in code** via transparent, tunable rules (e.g. white→DeLorean;
small+thin+harness→Scrappy). Ambiguous evidence maps to a review bucket rather
than a confident wrong guess.

## Consequences
- Cars went from 0 DeLorean / 224-in-review to 170/93 with ~97% precision; dogs
  recovered to 100% Scooby / 89% Scrappy recall.
- Mapping logic is inspectable and adjustable without re-prompting (e.g. the
  harness-weight tuning was simulated on logged observations, no model re-run).
- Scale cues are unreliable on bounding-box crops (everything fills the frame),
  so rules must lean on scale-invariant features (proportions, pattern, harness).
- Same principle will apply to any future identity types (incl. faces).
