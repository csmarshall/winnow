# Architecture Decision Records

> The distilled principles these records add up to live in
> [`../TENETS.md`](../TENETS.md) — the v1 front-door statement.


Short records of the significant, hard-to-reverse decisions behind Winnow — the
context, the choice, and the consequences — so the *why* survives even when the
code changes. One file per decision, numbered, append-only (supersede rather than
edit history).

Format: [`_template.md`](_template.md). Status ∈ Proposed | Accepted | Superseded.

## Index

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-model-assisted-human-curation.md) | Model-assisted, human-in-the-loop training-data curation | Accepted |
| [0002](0002-perception-vs-labeling-split.md) | VLM does perception; code assigns identity | Accepted |
| [0003](0003-verdicts-as-source-of-truth.md) | Verdicts are the source of truth; Frigate dataset is a projection | Accepted |
| [0004](0004-companion-not-fork.md) | Ship as a Frigate-native companion, not a fork or core PR | Accepted |
| [0005](0005-auth-on-as-primary-target.md) | Target auth-ON Frigate; client is auth-optional | Accepted |
| [0006](0006-diversity-and-hard-negatives.md) | Diversity over volume; rejections become hard negatives | Accepted |
| [0007](0007-always-ready-daemon.md) | Always-ready daemon with recurring refresh + cookie position | Accepted |
| [0008](0008-binary-sweep-manual-mode.md) | Binary-sweep manual (no-AI) mode, unified with AI mode | Accepted |
| [0009](0009-full-scene-from-frigate-snapshot.md) | Full-scene context + box from Frigate's own snapshot (not a self-drawn overlay) | Accepted |
| [0010](0010-one-best-frame-per-event.md) | One best frame per event (intra-event dedup) | Accepted |
| [0011](0011-reassign-and-subtype-creation.md) | On-the-fly reassignment and new-subtype creation | Accepted |
| [0012](0012-source-agnostic-core-frigate-first.md) | Source-agnostic core, Frigate-first reality (adapter contract) | Accepted |
| [0013](0013-user-triggered-commit.md) | Commit is user-triggered, not automatic | Accepted |
| [0014](0014-verdict-model-reassign-first-class.md) | Verdict model: reassign first-class, "no" is unresolved, reject deletes | Accepted |
