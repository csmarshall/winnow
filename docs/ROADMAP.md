# Roadmap / backlog

Tracked follow-ups. When the GitHub repo exists, these become issues.

## Event-level review (collapse per-event frame redundancy) — DONE (ADR-0015)
Shipped: one review card per event; on commit, a capped diverse keep-set
(`WINNOW_KEEP_PER_EVENT`, default 3) is categorized and the rest pruned from the train
pool via `POST /classification/{model}/train/delete`. The lightbox shows the keep-set
filmstrip to hand-validate the frames are the same entity. Follow-up below.

## Validate that per-event pruning doesn't hurt model accuracy
ADR-0015 deletes redundant train-pool crops — curtailing input data. The intent
(diversity over near-duplicate volume) *should* help generalization, and only *new*
per-event additions are capped (the existing dataset is untouched), but confirm it
empirically:
- **Measure** the model's agreement with human verdicts on the next batch of real
  detections after a curated retrain (a cheap accuracy proxy). If Frigate exposes a
  classification **reprocess**, reprocess a held-out batch and compare predictions.
- **Tune** `WINNOW_KEEP_PER_EVENT` from the result (raise if under-trained, lower if
  overfitting).
- **Safety option:** archive pruned crops (move aside) instead of hard-deleting, so a
  bad run is recoverable — a first-run guardrail before trusting the delete.

## Switch face library reassign to v0.18 `reclassify` when available
ADR-0016 ships with an adaptive client: on Frigate v0.17 (no
`POST /faces/{name}/reclassify`), a library face reassign is two calls —
`register` (upload to new person) + `delete` (remove from old). v0.18+ has the
one-shot endpoint, which we already probe for. When the running Frigate
exposes it, no caller change is needed — but the fallback branch and the probe
itself can be deleted as dead code. Trigger: GitHub Actions on Winnow can grep
the running Frigate version; or this is just a manual cleanup once you've
upgraded.

## Frigate best-practices alignment (2026-05-29 audit)
Audit of Winnow against Frigate's docs surfaced gaps to close (tracked as tasks #8–#11):
- **Cross-event / per-identity diversity sampling (HIGH, Gap 1).** Frigate: *"diversity
  matters far more than volume"*, *"keep largest class within ~3× the smallest."* The
  live commit path only dedups *within* an event — no cross-event near-dup drop, no
  per-identity cap, no camera/hour spread. The logic exists in legacy `promote.py`
  (`diverse_sample`: avg-hash dedup + cap + camera/hour spread) but it is **dead code**.
  Port it into `commit.plan_actions()` per `(model, identity)`; add `WINNOW_MAX_PER_IDENTITY`
  + dedup knobs + a 3× class-balance warning; then retire `promote.py`. (ADR-0006 carries
  an honesty note that this isn't wired yet.)
- **Train on harder/low-score frames, not just high-confidence ones (HIGH, Gap 2).**
  Review is low-confidence-first, but the *committed* representative/keep-set skews to the
  highest-confidence crop — the overfitting anti-pattern Frigate warns against
  (*"focus on relatively clear images that score lower"*).
- **Face per-person cap + pose/lighting diversity (MED-HIGH, Gap 3).** Frigate: *"4-6
  similar images max"*, *"20-30 per person"*, vary pose/lighting/day-night. Winnow has no
  per-person cap or diversity governance for faces today.
- **Smaller items (MED/LOW, Gaps 4-7):** surface Frigate's thresholds (recognition/
  unknown/detection) for reviewer context; small-vs-large model guidance; crop-quality
  gate (<100×100, blur, IR-for-faces); deliberate `none`-bucket diversity.
Source-agnostic split: caps/dedup/balance/quality-gates belong in the **core**; exact
numbers/thresholds/`model_size` belong in the Frigate **adapter**.

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
