# Contributing to Winnow

Thanks for your interest! Winnow is a small, dependency-light tool — contributions
that keep it that way are very welcome.

## Running the tests

Runtime is stdlib-only, so there's nothing to install:

```sh
PYTHONPATH=adapters/frigate:winnow python3 -m unittest discover -s tests -p 'test_*.py'
```

CI runs these on Python 3.11–3.13 for every push and PR.

End-to-end UI tests (optional, dev-only) live in `tests/e2e/` and use Playwright —
see `tests/e2e/README.md`. They are not part of the runtime image.

## Architecture

Winnow is a **source-agnostic core** plus **adapters**:

- `winnow/review_app.py` — the swipe UI + decision store. Stdlib only. It serves
  `review/candidates.jsonl`, records `review/verdicts.jsonl`, and calls two injected
  hooks: `REFRESH_FN` (pull fresh candidates) and `COMMIT_FN` (push decisions back).
  It knows nothing about Frigate.
- `adapters/frigate/` — the Frigate adapter: discovers models from Frigate's config,
  builds candidates, and commits verdicts through Frigate's API. A new backend is a
  new adapter that produces the candidate schema and wires the two hooks.

Decisions are recorded as Architecture Decision Records in `docs/adr/`; the guiding
principles are distilled in `docs/TENETS.md`. If you change behavior, add or update
an ADR.

## Conventions

- **No real data in the repo.** Model/person/pet names live only in your running
  Frigate instance. Tests and docs use fictional examples (Scooby/Scrappy, the
  Batmobile/DeLorean, Mario/Luigi/Peach). Never commit real names, host IPs, or
  credentials — put per-host values in `docker-compose.override.yml` (gitignored).
- Keep the runtime zero-dependency (stdlib + the browser). Dev tooling is fine.
- Match the surrounding code's style; `ruff` config is in `pyproject.toml`.
- Self-documenting names; comment the non-obvious (especially Frigate quirks).

## Submitting changes

1. Branch, make the change, add/adjust tests.
2. Ensure the suite passes locally.
3. Open a PR describing the behavior change and linking any relevant ADR.
