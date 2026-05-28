# ADR-0009: Full-scene context (and its box) comes from Frigate's own snapshot

- **Status:** Accepted
- **Date:** 2026-05-27

## Context
The swipe card shows a tight crop; tapping it opens the full scene so the
reviewer can judge *which* object is being classified — essential when several
people or dogs share a frame. That full scene needs a box marking the tracked
object. Two self-drawn approaches were tried and both failed on real data:

- **Recording frame at event start + the event's best-frame box (`data.box`):**
  on long / multi-object events the object had moved, so the box landed in empty
  space or on the wrong subject (Mario flagged 24s/43s/438s events).
- **Recording frame at the exact detection instant + box reconstructed from the
  trajectory (`path_data` centroid sized by `data.box`):** correct on cameras
  whose detect and record streams share an aspect ratio (the ultrawide back
  yard), but **wrong on the fisheye side-door doorbell**. Frigate's normalized
  coordinates live in the dewarped *detect* space; drawn onto the separately
  dewarped *record*-resolution frame (1200×1600 portrait) they pointed nowhere
  near the subject — Mario flagged his own face, where both the reconstructed
  box *and* the raw `data.box` sat center-left while he was at the right edge.

The root cause: any frame+box we assemble ourselves must reconcile two
coordinate spaces that differ per camera (and per lens dewarp).

## Decision
For **every** kind (dogs, cars, faces), the full scene is **Frigate's event
snapshot requested with `bbox=1`**. Frigate renders the frame and the box
together in one coordinate space, so the box is correct on every camera,
including fisheye. Winnow does **not** draw its own boxes or fetch arbitrary-time
recording frames. Frigate's UTC timestamp overlay is suppressed (`timestamp=0`,
the camera's own local OSD already shows time; `WINNOW_SNAPSHOT_TIMESTAMP` to
re-enable).

## Consequences
- Correct boxes on all cameras; the fisheye doorbell works.
- The frame is Frigate's *best frame*, not necessarily the instant the crop/face
  was captured — the box marks the tracked object, which is the identity in
  question. Genuine Frigate track-swaps in crowds remain (rare) and are flaggable.
- Detect-resolution (smaller — e.g. 360×480 for the doorbell), but the clip in
  the lightbox gives record-resolution detail on demand.
- The superseded approaches (recording-frame overlay, `path_data` reconstruction,
  client-side box overlay) were removed; the `.lbbox` overlay hook is dormant.
