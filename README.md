# 🌾 Winnow

**Swipe your training data clean.**

Winnow is a human-in-the-loop tool for building *known-good* training datasets.
It surfaces a queue of candidate images — each with a proposed label — and you
confirm, reject, or reassign each one with a Tinder-style swipe. The confirmed
images become a curated dataset that's pushed back to retrain the model, and
because it's a loop, every cycle the model gets better and you have less to
review.

> *To winnow* = to separate the grain from the chaff. That's the whole job: keep
> the good examples, toss the bad ones, with a human in the loop where it matters
> and a model doing the grunt work everywhere else.

The first (and so far only) adapter targets **[Frigate](https://frigate.video)**:
it curates training data for Frigate's custom object classifiers (pets, cars) and
its face-recognition library (people).

---

## Why it exists

Labeling training data by hand is miserable — clicking through a UI one image at
a time. But fully automatic labeling is wrong often enough that you can't trust
it. Winnow splits the work the way it should be split:

- **The model does perception at scale** — "what color is this car", "which dog
  is this" — cheaply and locally.
- **You do judgment** — a fast yes/no swipe, lowest-confidence-first, so your
  attention lands where the model is weakest.
- **The loop compounds** — confirmed data retrains the model, the next batch
  needs fewer corrections, repeat every few weeks.

The hard-won design lesson baked in (see
[ADR-0002](docs/adr/0002-perception-vs-labeling-split.md)): **give the model
perception tasks (color, size, shape) and do identity/labeling in code.** Asking a
VLM to "identify the DeLorean" makes it hedge ("can't read the badge") and
hallucinate; asking "what color is the most prominent vehicle" and mapping
white→DeLorean in code works.

---

## How it works

Winnow runs as an **always-ready daemon in a Docker container**. A non-technical
reviewer just opens the page, sees the pools, and swipes — no CLI, no API
knowledge. There's no separate "pull / classify / promote" dance; a **refresh**
(on a timer and on demand via "Check for new") does it all:

1. **Commit** previously-confirmed verdicts back to Frigate (categorize + retrain).
2. *(optional)* **Pre-sort** the train pool with a local VLM so the likely answer
   is already proposed.
3. **Rebuild** the review queue from Frigate's current data.

Two modes:
- **Manual (no-AI):** one review card per Frigate *event* — "Is this *X*?" (X = the
  best frame's guess) — confirmed, reassigned, or skipped. Deciding an event keeps a
  small diverse set of its frames for training and prunes the near-duplicate rest
  ([ADR-0008](docs/adr/0008-binary-sweep-manual-mode.md),
  [ADR-0015](docs/adr/0015-event-level-review-and-keepset.md)).
- **AI pre-sort:** a local VLM (Qwen2.5-VL via Ollama) proposes a label first, so
  most swipes are a one-tap confirm.
- **Library cleanup:** review what Frigate has *already committed* (the face
  library + classifier datasets) and fix the high-confidence mis-matches that
  bypassed the train pool — same swipe loop, same keys, no need to leave Winnow
  ([ADR-0016](docs/adr/0016-library-curation.md)).

The swipe UI:
- A **5-way button cross** (✗ No · 🏷 Reassign · ✓ Yes · Skip · Undo/Back) that
  mirrors the arrow keys (and WASD); tap an image for the **full scene with Frigate's
  box** (and, for an event card, a filmstrip of the frames that will be trained).
- A **"?" reassign** typeahead — when the yes/no doesn't fit, reassign to another
  subtype or create a new one, keyboard-navigable and case-insensitive
  ([ADR-0011](docs/adr/0011-reassign-and-subtype-creation.md)).
- **Flag** anything broken for later review; per-pool progress bars; resumable.

**Verdicts are the source of truth** ([ADR-0003](docs/adr/0003-verdicts-as-source-of-truth.md)) —
Frigate's dataset is a disposable projection, (re)built by the commit step.

---

## Architecture

Winnow is a small **core** plus per-source **adapters**. The core is stdlib-only
and imports nothing source-specific — it serves a queue of *candidates* and
records *verdicts*, driven entirely by an adapter's refresh hook.

In practice, **Winnow is Frigate-first**: there's one adapter, the abstraction is
untested against a second source, and a few Frigate concepts have settled into the
candidate schema. [ADR-0012](docs/adr/0012-source-agnostic-core-frigate-first.md)
gives the honest assessment and the exact contract a second source must satisfy —
don't read "source-agnostic" as "already works with anything."

```
   Frigate            ADAPTER  (adapters/frigate/)        CORE  (winnow/)
 ┌──────────┐     ┌──────────────────────────────┐   ┌────────────────────┐
 │ HTTP API │◀───▶│ frigate_client               │   │ review_app.py      │
 │ /config  │     │ discover_models (auto)        │   │  candidates.jsonl  │
 │ /events  │     │ classify (VLM pre-sort, opt) ─┼──▶│   ─▶ swipe UI       │
 │ /classifi│     │ build_candidates              │   │      ▼             │
 │ /faces   │◀───▶│ commit (categorize + retrain) │◀──┤  verdicts.jsonl    │
 └──────────┘     │ daemon (refresh hook) ────────┼──▶│  (REFRESH_FN)      │
                  └──────────────────────────────┘   └────────────────────┘
```

- **`winnow/review_app.py`** — the core. Zero-dependency (stdlib `http.server` +
  vanilla JS) swipe app. Reads `review/candidates.jsonl`, writes
  `review/verdicts.jsonl`. Undo, resume, reassign, LAN-accessible.
- **`adapters/frigate/`** — talks to Frigate's **HTTP API** (no DB access),
  discovers models, optionally pre-sorts with a VLM, and commits confirmed results
  back via the API. Wired to the core by `daemon.py`.

### Data contract (what an adapter produces)

`review/candidates.jsonl`, one JSON object per line — e.g.:

```json
{"cid":"Scooby|Scooby|177...","kind":"dog","identity":"Scooby","model":"Scooby",
 "img_url":"http://frigate/...","full_url":"http://frigate/...","confidence":0.9,
 "reason":"brindle, stocky","source":"classifier","meta":{}}
```

The core groups by `kind`→`identity`, serves the queue by `cid`, and records
`{cid, verdict}` (verdict ∈ `yes | no | skip | assign:<target>`). Adapters carry
opaque source-specific fields (Frigate uses `model` / `training_file` /
`face_train`) and may write `review/targets.json` for the reassign picker. Full
contract — core-used vs. pass-through fields — in
[ADR-0012](docs/adr/0012-source-agnostic-core-frigate-first.md).

---

## The Frigate adapter

Curates training data for a Frigate NVR over its **HTTP API** (read + write — no
SQLite access, no sudo install scripts; that was the old prototype). Highlights:

- **Auto-discovery:** dog/car classifiers and their categories are read from
  Frigate's own `/api/config` + datasets at refresh time — **no model names are
  hardcoded**. Add a classifier in Frigate and it appears in Winnow automatically.
  People come from the face-recognition library.
- **Commit is API-native and idempotent:** confirmed classifier images are
  `categorize`d into the model's dataset; confirmed faces are classified under
  their name; then the model retrains. Dry-run by default; every action logged so
  re-runs skip what's done ([ADR-0004](docs/adr/0004-companion-not-fork.md)).
- **Full-scene context with the *right* box:** the lightbox shows Frigate's own
  event snapshot with its box, which is correct on every camera — including
  fisheye doorbells where self-drawn overlays land on the wrong subject
  ([ADR-0009](docs/adr/0009-full-scene-from-frigate-snapshot.md)).
- **Auth-optional:** works against the unauthenticated `5000/api` port on a
  trusted LAN, or an authed `8971` HTTPS instance with credentials
  ([ADR-0005](docs/adr/0005-auth-on-as-primary-target.md)).
- IR/night frames collapse color cues, so the VLM pre-sort routes them to a review
  pile instead of guessing ([ADR-0006](docs/adr/0006-diversity-and-hard-negatives.md)).

The review decision flow per model type (classifier vs face recognition) is drawn
in [`docs/flows.md`](docs/flows.md).

---

## Quick start

Winnow ships as a Docker container ([ADR-0007](docs/adr/0007-always-ready-daemon.md)).
From the project root:

```bash
# copy the override template and set your values (gitignored — never edit the base file):
#   cp docker-compose.override.yml.example docker-compose.override.yml
#   then set FRIGATE_URL (browser-reachable LAN IP) and, when ready, WINNOW_NO_COMMIT: "0"
sudo docker compose up -d --build
```

Then open **http://YOUR_HOST:8077** on your LAN and start swiping. The container
auto-restarts on boot (`restart: unless-stopped`).

- **Manual mode** (`WINNOW_MANUAL=1`) needs nothing but Frigate. **AI pre-sort**
  additionally needs **Ollama** (`qwen2.5vl:7b`); without it the app still serves
  and accepts swipes (refresh just won't pre-classify).
- `WINNOW_NO_COMMIT=1` reviews without writing anything back to Frigate (testing).
- See [`deploy/README.md`](deploy/README.md) for details and
  [`docs/SETUP.md`](docs/SETUP.md) for auth modes.

---

## A real run (anonymized)

Against the homelab Frigate, the VLM pre-sort shows the perception-first approach
holds up in daylight; night/IR is the open hard problem:

| Identity type | result |
|---|---|
| 🐕 Dogs | Scrappy / Scooby split cleanly; ~⅓ routed to review (mostly IR/night) |
| 🚗 Cars | DeLorean / Batmobile ~97% precision; a handful to review (IR/night) |
| 🙂 People | from Frigate's face library (Mario, Peach, Zelda, …) |

(Identity names here are fictional placeholders — real model/person names live
only in your Frigate instance, never in this repo.)

---

## Testing

- **61 stdlib unit tests** (`python3 -m unittest discover -s tests`) — a
  `FakeClient` stands in for Frigate, so nothing touches the network or Ollama.
- **Dev-only browser smoke suite** ([`tests/e2e/`](tests/e2e/)) — Playwright
  coverage of the critical UI paths (reassign, voting, flagging). *Not* a runtime
  dependency; the daemon/image stay zero-dep.

---

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md). Highlights:

- [ ] Make the core fully **kind-agnostic** (kinds/labels from candidates, not a
      hardcoded person/dog/car taxonomy — a leak point noted in ADR-0012).
- [ ] **Cloud-model adjudication** for the low-confidence / IR review pile.
- [ ] A **second adapter** to actually prove generality (a plain image folder, or
      another NVR).
- [ ] **Standalone (non-Docker)** install instructions.
- [ ] **Package it** — bare `winnow` is taken on PyPI (abandoned 2015 pkg), so the
      core would ship as `winnow-core` and the adapter as `winnow-frigate` (import
      name can stay `winnow`).
- [x] Auto-discover models from Frigate (no hardcoded names).
- [x] Always-ready daemon + on-the-fly reassignment + new-subtype creation.

The principles behind it are distilled in [`docs/TENETS.md`](docs/TENETS.md);
the full decisions are recorded as [ADRs](docs/adr/).

## Frigate references — the best practices Winnow follows

Winnow optimizes Frigate the way Frigate asks to be optimized; the relevant docs:

- [Custom classification](https://docs.frigate.video/configuration/custom_classification/) ·
  [Object classification](https://docs.frigate.video/configuration/custom_classification/object_classification)
  — train from the **Recent Classifications** tab, *"gather balanced examples across
  times of day, weather, and distances"*, *"keep classes visually distinct"*, and the
  auto-`none` hard-negative bucket. (Winnow's event-level keep-set, ADR-0015, exists to
  honor "balanced/diverse, not near-duplicate volume".)
- [Face recognition](https://docs.frigate.video/configuration/face_recognition) —
  identity by face embedding with *"different poses, lighting, and expressions"*; no face
  negatives (Winnow's `reject` deletes rather than trains a negative — ADR-0014).

---

Built with [Claude Code](https://claude.com/claude-code).
