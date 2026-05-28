# Community posts (DRAFT — review before posting)

> Context: Frigate #21508 (bulk select/assign) and #20398 (custom classification
> multi-select) are both **closed as not planned** — bulk-assign UX won't go into
> core. So this is NOT a feature request. It's (1) a Show-and-Tell announcing a
> companion tool, and (2) a short pointer comment on the closed #21508 for people
> who find it via search.
>
> POST TIMING: only after native Winnow is built and there's a public repo to
> link. Fill in the REPO_URL placeholders first.

---

## 1. GitHub Discussion → "Show and tell"

**Title:** Winnow — model-assisted bulk review for Frigate classification & face training (companion container)

**Body:**

After training a few custom classifiers (two dogs, two cars) and tidying my face
library, I hit the same wall others have: categorizing examples **one image at a
time** is slow once you have hundreds of them. Rather than ask for changes in
core, I built a small **companion** that drives Frigate's existing API.

**What it does**
- Connects to a Frigate instance over the network — running **alongside Frigate
  or on a separate box** (e.g. one with a spare GPU).
- Lists a model's un-categorized `train/` images via the API.
- *Optionally* pre-buckets them with a **local** vision model (Qwen2.5-VL via
  Ollama) so most images come pre-sorted — you mostly confirm.
- Presents a fast **swipe / bulk-review** UI ("is this Scooby? ←/→"), lowest-
  confidence first.
- Commits in one batch via `categorize` / `reclassify`, then triggers
  `POST …/train`. Review is fully local until you commit — fix freely, then one
  push.
- Same flow for the **face library** (confirm/clean via the faces endpoints).

**Design promises (on purpose)**
- Uses **only existing, documented API endpoints** — no Frigate changes, no fork.
- The VLM is **strictly optional**: with it off, Winnow is just a plain bulk
  reviewer over your `train/` pool. (So the heavy dependency is never required.)
- Auth-aware: works with auth on (login → token) or off.

**Why post this here:** a couple of bulk-assign requests were closed as not
planned for core, but the demand is clearly real — so I'm sharing this as an
external option for anyone with large datasets, and to gather feedback.

Repo: REPO_URL  ·  early/prototype, issues & ideas welcome.

One question for the maintainers if you have a moment: **is the
`/api/classification/...` surface something you consider stable enough to build
companions against**, or still in flux? Happy to adapt.

---

## 2. Short comment on the (closed) #21508

> For anyone landing here still wanting bulk select/assign: since this is
> closed-as-not-planned for core, I put together an external companion that does
> bulk + (optional) model-assisted review against Frigate's existing
> classification API (`generate_examples` → `train/` listing → `categorize` →
> `train`). Runs in a container against a local **or** remote Frigate; the model-
> assist is optional so it also works as a plain bulk reviewer. Sharing in case
> it helps others with large datasets: REPO_URL

---

## Notes for us (not for posting)
- Lead with humility + "I built this," never "you should add X."
- Keep it short; maintainers/community skim.
- Don't overstate maturity — call it prototype.
- The API-stability question is the only thing we actually want from maintainers;
  everything else is for users.
