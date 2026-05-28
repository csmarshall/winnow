# ADR-0007: Always-ready daemon with recurring refresh

- **Status:** Accepted
- **Date:** 2026-05-26

## Context
The end user is a non-technical reviewer (Mario's daughter) who should just
open a page and swipe — no CLI, no commands, no understanding of Frigate or APIs.
The earlier flow required running `classify` / `build_candidates` / `commit` by
hand. We need an "always-ready" form: open it anytime, see what's there, make
progress, and pull more when caught up.

## Decision
Run Winnow as a **persistent daemon** (the review server stays up; shipped as a
Docker container with `restart: unless-stopped`). The source-agnostic core exposes a pluggable
**refresh hook**; the Frigate adapter supplies it. A refresh does, in order:
**commit** confirmed verdicts → **generate_examples + classify** the train pool →
**rebuild** the queue. Refresh runs:
- **on a timer** (default 30 min) so pre-sorted snapshots are usually already
  waiting, and
- **on demand** via a "Check for new" button shown when the reviewer is caught up.

The home page summarizes "**X pools across Y types**" with per-pool progress and a
freshness footer ("refreshed N min ago"). Committing is **automatic** (part of
refresh) so the reviewer never touches Frigate.

Per-reviewer **"where am I" position is a short-TTL (~1h) client cookie**, not
server state: the durable decisions live in `verdicts.jsonl` (server), while the
ephemeral "which pool / resume here" is per-device and self-expiring — so
multiple people can review independently and nobody has to log in.

## Consequences
- The reviewer's loop is: open page → swipe pools → "Check for new" → repeat.
  No CLI, no API exposure; commits + retrains happen invisibly.
- The core stays source-agnostic (refresh is injected); the adapter owns the
  Frigate-specific work. Same boundary as ADR-0004.
- Auto-refresh runs the VLM on a timer — needs Ollama up and accepts that cost;
  retrain only fires when there are new confirmations (commit gates it).
- `img_url` must be the **browser-reachable** Frigate address (LAN IP), since
  it's embedded for the client — not `127.0.0.1`.
- Cookie position is best-effort; because the queue serves only un-decided items,
  reopening a pool naturally resumes at the next pending image regardless.
- The cookie is pinned to a **`build_id`** (the candidate-set timestamp): a
  rebuild/clear/refresh changes it, so a stale "Resume X" pointing into a wiped or
  regenerated dataset is discarded rather than offered; it survives a benign
  process restart.
