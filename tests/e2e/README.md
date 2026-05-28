# Browser smoke tests (dev-only)

A small Playwright suite covering the critical UI paths that the stdlib unit tests
can't reach — the things that have actually regressed: the reassign button/`r`
key going dead (queue payload missing `kind`) and clicking a name doing nothing
(broken `onclick` quoting).

**This is a development/CI dependency only.** Playwright + a browser binary are
*not* part of the Winnow runtime or the Docker image — the daemon stays
zero-dependency. The tests run the real `review_app` server in-process against a
temp `review/` dir seeded with fictional data (see `conftest.py`).

## Run

```bash
pip install -r tests/e2e/requirements-dev.txt --break-system-packages
playwright install chromium          # one-time browser download
pytest tests/e2e -q
```

## What it covers

- a pool opens and the inverted-T button cross renders
- yes / skip / no post the right verdict
- flag posts to `/api/flag`
- the 🏷 reassign button exists on a person card and opens the dialog
- the typeahead filters (shared-prefix: Toad / Toadette)
- arrow-key highlight + Enter posts `assign:<name>`
- clicking a name posts `assign:<name>` (the onclick-quoting regression)
- a blank field pre-highlights nothing
