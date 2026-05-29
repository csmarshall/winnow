# Winnow — Tenets

The principles Winnow is built on, distilled from the [ADRs](adr/) ahead of the
first real release. The ADRs remain the detailed history and rationale; these
tenets are the front-door statement of *what Winnow is and why*. When a future
decision is unclear, it should be resolved in favor of these.

---

### 1. The human is the authority; the model is an accelerator.
The model pre-sorts at scale and surfaces the **lowest-confidence first**, so human
attention lands where the model is weakest — but **nothing is "known-good" until a
human confirms it**. The human **verdicts** are the durable source of truth; the
source's training data is a disposable **projection** of those verdicts, rebuilt on
commit. <sub>ADR-0001, 0003</sub>

### 2. Models perceive; code decides.
Constrain the model to neutral, **observable perception** (color, build, "is there a
harness?", "whose face?") and do **identity, labeling, and counting in transparent,
tunable code**. Asking a model to *name* things makes it hedge and hallucinate;
asking it to *describe* them and mapping to identity in code is inspectable and
fixable. Ambiguous evidence routes to review, never a confident wrong label.
<sub>ADR-0002</sub>

### 3. Optimize the host from inside it — companion, not fork.
Speak the source's **native API** and follow **its own workflow and rendering**: its
train pool, its categorize/classify endpoints, its dataset structure, its drawn
boxes. Don't fork it, don't build a parallel pipeline, and don't reinvent what it
already does well. A small **source-agnostic core**, one **adapter** per source that
talks the source's language. <sub>ADR-0004, 0009, 0012</sub>

### 4. Honor the source's best practices.
Curate the way the source *wants* to be trained: **diversity over volume**,
hard-negatives only where the source supports them (the classifier `none` bucket —
**never** face negatives, which Frigate doesn't do), **low-confidence-first**, and
**a small, capped, diverse set per event** (one review card per event; keep a few
timeline-spread frames, prune the rest) so near-duplicates don't cause fatigue or
overfitting. Auth-optional, but capable of the source's strict auth.
<sub>ADR-0005, 0006, 0010, 0015 · Frigate's own guidance:
[object classification](https://docs.frigate.video/configuration/custom_classification/object_classification)
("gather balanced examples across times of day, weather, and distances"; "keep classes
visually distinct"),
[face recognition](https://docs.frigate.video/configuration/face_recognition)
("different poses, lighting, and expressions").</sub>

### 5. Capture signal; never silently discard it.
Every outcome teaches the system. **Confirm**, **reassign/identify** (the first-class
correction — *the* way to fix a wrong guess), **reject** (delete the noise from the
source), or **defer** — and **surface what the source couldn't classify** ("who is
this?") rather than dropping it. A "no" defers or parks; it never throws an identity
away. <sub>ADR-0006, 0008, 0011, 0014</sub>

### 6. The reviewer controls the loop — and it's effortless.
An **always-ready daemon** a non-technical person just opens and swipes: one image,
one gesture, a UI that **mirrors the keys** and resumes where you left off. Decisions
accumulate locally; pushing them to the source is an **explicit, batched commit**
(review → fix → push), never an automatic side-effect. <sub>ADR-0007, 0008, 0013</sub>

---

Every ADR maps to a tenet: 0001→1, 0002→2, 0003→1, 0004→3, 0005→4, 0006→4/5,
0007→6, 0008→5/6, 0009→3, 0010→4, 0011→5, 0012→3, 0013→6, 0014→5, 0015→4/6, 0016→3/5/6.
