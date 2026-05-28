# ADR-0013: Commit is user-triggered, not automatic

- **Status:** Accepted
- **Date:** 2026-05-27
- **Supersedes:** the "committing is automatic (part of refresh)" decision in ADR-0007

## Context
ADR-0007 made commit a silent side-effect of every refresh: "Committing is
automatic (part of refresh) so the reviewer never touches Frigate." In use that's
wrong on two counts:

- It contradicts ADR-0003 (verdicts are the source of truth; Frigate's dataset is
  a *projection* built by an explicit step) — auto-commit pushes the projection
  out from under the reviewer continuously.
- It defeats "fix mistakes, then commit in one push." Decisions hit Frigate (and
  trigger retrains) piecemeal as you go, so there's no clean point to review and
  correct before anything is written.

Per-section commit prompts ("commit this pool now / wait") were considered and
rejected: they nag, and they make the reviewer track process state instead of just
reviewing.

## Decision
**Commit is an explicit, user-triggered action.** Verdicts accumulate locally
(`verdicts.jsonl`) and nothing is pushed to the source until the reviewer acts.

- The core exposes a **`COMMIT_FN`** hook (alongside `REFRESH_FN`) + a
  `POST /api/commit` endpoint; the adapter supplies the push-and-rebuild.
- **Refresh no longer commits** — it only brings in fresh candidates.
- The home screen **leads with progress**, mirroring the per-pool "X left" wording:
  *"X of Y sub-categories to review before committing them to `<source>`,"* with a
  prominent **Commit** button once everything's reviewed (and a low-key
  "commit N done now" for partial pushes). No per-section nagging.
- On commit, the adapter pushes confirmed verdicts and **rebuilds** — committed
  (categorized) train files move out of the pool, so the pools naturally reset to
  the leftovers + anything new. `no`/`skip` keep their verdicts and stay decided.
- The source's display name is injected (`SOURCE_NAME`) so the core stays
  source-agnostic (ADR-0012).

## Consequences
- The reviewer controls when work lands in the source — review, fix, then push in
  one batch.
- Commit is idempotent (logged to `committed.jsonl`); `WINNOW_NO_COMMIT=1` makes
  the commit action a dry-run (nothing written) for safe shakedowns.
- Retrains fire on the explicit commit, not on every refresh — fewer, intentional.
- ADR-0007 stands except for its automatic-commit clause, superseded here.
