# Roadmap / backlog

Tracked follow-ups. When the GitHub repo exists, these become issues.

## Event-level review (collapse per-event frame redundancy)
Frigate saves many near-identical crops per tracked object — one parked car can land
**9+ frames** in the train pool. Dedup currently shows one crop per event *per build*,
but the unreviewed siblings linger and resurface across commits (review one, commit,
the next frame of the same car appears), and categorizing many near-identical frames
over-weights that event — against Frigate's own "diversity over volume, cap similar
images" guidance.

Fix: review at the **event** level. When a representative crop is decided, apply the
decision to the whole event in one pass — categorize one representative and **clear the
redundant siblings** from the train pool (needs a train-crop delete on the client; today
it only has `categorize`). One review per event, no resurfacing, diverse training.

## v1 "tenets" consolidation
Before the first real release, re-evaluate ADRs 0001–0014 and distil them into a
cohesive, short set of guiding **tenets** (the principles the system is built on),
rather than 14 separate decision records. The ADRs stay as the history; the tenets
become the front-door statement of what Winnow is and why.

## Verdict model (ADR-0014) — build-out
Implement the full model: dog/car reassign **reshuffle** (no source + yes target
sibling), face **Unresolved** bucket (route "no" there, revisitable), reassign as a
**first-class** action, and **reject → delete** from the source. Partially staged.

## Consolidate per-identity binary classifiers into one multi-class model (e.g. Cars)
Mutually-exclusive identities (the two cars; like the dogs) are better as **one
multi-class classification model** than several single-class binary ones: one review
pool, reassign works (kind-agnostic, ADR-0014), and the model learns to *discriminate*
between them rather than each binary only learning "this-one vs. everything".

**No data is lost** — the old models' datasets stay on disk until you delete them.
Two migration paths (Frigate-side; classifier dataset dirs are root-owned):

1. **Preserve via filesystem copy (no re-labelling).**
   - In Frigate, create a new multi-class model on the `car` object with classes
     `Batmobile`, `DeLorean` (Frigate auto-adds `none`).
   - Copy each old model's *positive* dataset into the new model's matching class dir,
     and merge both `none` dirs, under Frigate's clips dir
     (`<clips>/<model>/dataset/<category>/`):
     ```
     <clips>/Batmobile/dataset/Batmobile/*        -> <clips>/Cars/dataset/Batmobile/
     <clips>/DeLorean/dataset/DeLorean/* -> <clips>/Cars/dataset/DeLorean/
     <clips>/Batmobile/dataset/none/* + <clips>/DeLorean/dataset/none/* -> <clips>/Cars/dataset/none/
     ```
     (sudo; the dirs are root-owned — Winnow can generate the copy script.)
   - Train the new model (`POST /classification/Cars/train`). A Frigate restart may be
     needed for it to index the copied files (train reads the dir, but the UI/DB may
     cache); verify the dataset counts before deleting anything.
   - Once the new model classifies well, delete the two old models.

2. **Re-sweep (supported flow, re-confirm).** Create the `Cars` model, let Frigate
   collect examples into its train pool (or `generate_examples`), and sweep them in
   Winnow — which now offers reassign for the multi-class model. Slower (you re-label
   ~270 crops) but uses only supported APIs; the old datasets remain as a safety net.

Recommended: try (1); fall back to (2) if Frigate doesn't index the copied files.

## Standalone (non-Docker) install instructions
Winnow is zero-dependency (stdlib `http.server` + Pillow/numpy), so it can run
directly under an init system without Docker. Document a supported standalone
path: a generic systemd unit (or equivalent), virtualenv/system-package setup,
and how to point it at Frigate + Ollama. Removed the half-baked
`deploy/winnow.service` in favor of the Docker path; this issue covers doing the
bare-metal path properly.

## Authenticated / remote Frigate (TLS)
The client supports `FRIGATE_USER`/`FRIGATE_PASSWORD` + the 8971 HTTPS port, but
TLS verification handling for a remote/authed instance is still TODO (see
docs/SETUP.md, ADR-0005).
