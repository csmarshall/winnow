# ADR-0017: Rescan recordings — harvest face data via human-confirmed re-recognition

- **Status:** Accepted
- **Date:** 2026-06-01
- **Follows:** ADR-0016 (library curation)

## Context
After trimming a bloated face library (ADR-0006 / Tenet 4), Frigate's face
recognition often works *better* on existing recordings than it did when those
events were originally captured — the cluster centroids have moved. So a face
that Frigate recorded as **`unknown`** (or worse, confidently labeled as the
wrong person, because one identity's cluster was dominant) can now be matched
to its actual owner. We confirmed this experimentally: one event was stored
with sub_label `<X>`; after the trim, `POST /api/faces/recognize` on the same
event snapshot returned `<Y>` at 0.99 confidence. Same image, same model;
different — and now correct — answer.

There's no native Frigate "rescan and re-classify the recording archive" — the
events DB keeps whatever sub_label was set at capture time. Without an action,
that historical labeling stays wrong forever, and a chunk of high-quality face
data in those recordings stays out of the library.

**But:** doing this *automatically* is dangerous. Our first dry-run found
~840 / 1934 events the trimmed library matched, with one identity attracting
59% of the matches. That's exactly the failure mode a pose-biased trim can
cause: when the kept crops for `X` happen to be mostly frontal, ArcFace embeds
*any* frontal face closer to X's centroid than to the more-side-shot kept
crops of other people. Auto-registering all 840 hits would re-bloat the
library with cross-identity poison.

## Decision
**Two-phase, human-in-the-loop rescan:**

- **Phase 1 — scan (`eval/rescan_recordings.py`):** walk recent person events,
  POST each event snapshot to `/api/faces/recognize`, emit ONE pending
  candidate per confident match into `review/rescan_candidates.jsonl`. No
  writes to Frigate. Idempotent (already-scanned event ids cached in
  `review/rescanned_events.txt`).
- **Phase 2 — review (Winnow `rescan` bucket):** `from_rescan` in
  `build_candidates.py` reads the pending file and surfaces each as a swipe
  card in a new **"🔍 Rescan candidates"** home-page section. The card asks
  *"Is this `<recognized>`?"* with the event snapshot as the full scene.
  - **Yes** → on commit, `POST /faces/<recognized>/register` with the event
    snapshot bytes. Frigate runs its own detection+alignment+embedding on the
    full frame; only crops passing `min_area` (now 10000) actually land. So
    captured at Frigate's native record resolution, not the historical
    detect-stream thumbnail.
  - **Reassign → `<Other>`** → register to `<Other>` instead. The most
    valuable correction: a face the trimmed library thought was X but is
    actually Y gets added to Y's library (and stays out of X's).
  - **No / Reject** → drop the candidate; nothing registered.

The candidate's `reason` line surfaces when the trimmed library *disagrees*
with the originally-stored sub_label, so the reviewer sees the disagreement up
front (extra scrutiny on those).

## Consequences
- Reclaims meaningful library data from 14 days of recordings without
  trusting a single recognition call. Each new library entry has explicit human
  confirmation.
- The `rescan` bucket joins the existing `review` (daily) and `library`
  (cleanup) buckets on the home page. Same swipe UI, different data source —
  no new UX to learn.
- We respect Frigate's threshold guidance by using `recognize`'s own score
  (default cutoff 0.85). Below that the candidate isn't emitted at all.
- Adding a bucket forced `plan_actions()` to introduce a new `rescan_registers`
  field; that's why it's a `namedtuple` now (ADR-0016) — extensible.
- Once Frigate v0.18 ships its one-shot `/faces/{name}/reclassify`, neither
  this flow nor the v0.17 library-reassign fallback need to change — they
  already use the high-level `face_register` / `face_reclassify` client
  methods, which adapt internally.

<sub>Tenets 1 (human authority), 3 (companion-not-fork), 5 (capture signal,
never silently discard it), 6 (effortless loop). Builds directly on the trim
work in ADR-0006 / ADR-0014 / ADR-0016 — the cleaner the library, the better
the recognize results, the more valuable this becomes over time.</sub>
