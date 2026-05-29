# Changelog

All notable changes to Winnow are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are [SemVer](https://semver.org/).

## [0.1.0] — 2026-05-28

Initial public release.

### Added
- **Swipe-to-curate UI** — a Tinder-style review app (zero-dependency stdlib
  `http.server` + vanilla JS) for confirming, reassigning, and rejecting Frigate's
  classification and face-recognition candidates.
- **Source-agnostic core + Frigate adapter** — `winnow/review_app.py` exposes
  `REFRESH_FN`/`COMMIT_FN` hooks; `adapters/frigate/` discovers models from Frigate's
  config (no hardcoded names) and commits via Frigate's native API.
- **Verdict model** (ADR-0014) — confirm (Yes), reassign (first-class), reject→delete,
  and a "Needs ID" pool for unrecognized faces.
- **Multi-class single-pick** — one card per crop ("Is this `<guess>`?") with a unified
  5-way swipe cross across dogs, cars, and people.
- **Reassign reshuffle** — a reassigned crop leaves its source pool and shows allocated
  (decided) in the target pool; committed to the target on commit.
- **User-triggered commit** (ADR-0013) — verdicts accumulate locally; Commit pushes
  them (categorize / classify / delete) and retrains the affected models.
- **Full-scene context** — the exact face-capture frame for faces, with a camera-probe
  fallback that recovers the scene even when a tracked event has aged out.
- **Event-level review** (ADR-0015) — one card per Frigate event; on commit, a capped
  diverse keep-set (`WINNOW_KEEP_PER_EVENT`, default 3) is trained and the redundant
  near-duplicate frames are pruned. The lightbox shows the keep-set filmstrip to verify
  the frames are the same entity before confirming.
- **Low-confidence-first** ordering and a dry-run mode (`WINNOW_NO_COMMIT=1`) for safe
  shakedowns.
- Docs: ADRs 0001–0015, `docs/TENETS.md`, `docs/flows.md`, `docs/SETUP.md`.

[0.1.0]: https://github.com/csmarshall/winnow/releases/tag/v0.1.0
