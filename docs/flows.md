# Decision flows (Frigate adapter)

How a candidate moves through review for each Frigate model type, per the verdict
model in [ADR-0014](adr/0014-verdict-model-reassign-first-class.md). Verdicts are
local until you **commit** (ADR-0013); the "commit:" boxes are what a commit pushes.

## Which path?

```mermaid
flowchart TD
  X[Frigate candidate] --> T{Model type}
  T -->|dog / car classifier| CLF["Event cards (one per tracked object)"]
  T -->|person / face recognition| FACE[Who-is-this flow]
```

## Classifier (dogs / cars) — event-level (ADR-0015)

One review card per Frigate event (its train crops grouped by tracked-object id),
shown as the best frame's guess. Deciding the event applies to the WHOLE event: a small
diverse keep-set is categorized to the chosen class and the redundant sibling frames are
pruned. Reassign is a single `assign:TARGET` verdict that `identities()` reshuffles — the
event leaves its source pool and is shown allocated in the target pool.

```mermaid
flowchart TD
  C["Event card — 'Is this SUBTYPE?' (best frame; N frames in event)"]
  C -->|Yes| KEEP["commit: categorize the keep-set into dataset/SUBTYPE, prune the rest"]
  C -->|"Reassign → TARGET (subtype / new / none)"| RA["assign:TARGET — allocated to the target pool"]
  RA --> KEEP2["commit: categorize the keep-set into dataset/TARGET, prune the rest"]
  C -->|No| PARK["not this — parked (revisit / reassign later)"]
  C -->|Skip| SKIP["parked — revisit"]
```

## Face recognition (people) — "who is this?"

Reassign is first-class. Frigate's unrecognized ("unknown") faces are surfaced for
identification rather than dropped — they merge with human-rejected guesses into a
**Needs ID** pool. Nothing Frigate detected is silently lost.

```mermaid
flowchart TD
  G{Frigate guessed a name?}
  G -->|yes| P["pool = the guess — 'Is this NAME?'"]
  G -->|no / unknown| NID["Needs ID pool — 'Who is this?'"]

  P -->|Yes| TRAIN["commit: train this face for NAME"]
  P -->|Identify| WHO{Who is it?}
  NID -->|Identify| WHO
  P -->|Skip| PARK["parked — revisit"]
  NID -->|Skip| PARK

  WHO -->|an already-known person| TX["commit: train for that person"]
  WHO -->|a new person to track| NEW["Create pool → train under the new name"]
  WHO -->|neither — not worth tracking| REJ{Recurring?}

  P -->|Reject| REJ
  NID -->|Reject| REJ
  REJ -->|"one-off / noise (passerby)"| DEL["commit: delete from Frigate's train pool"]
  REJ -->|"recurring (FedEx, mailman)"| CATCH["Identify → catch-all 'Delivery' / 'Stranger'"]
```

**The key branch** (your point): an unrecognized face is *known* → identify to that
person; or the *seed of a new pool* → create; or *neither* → reject. "Reject" is the
residual after ruling out both — and a recurring non-household face (FedEx) is worth
its own catch-all bucket rather than rejecting it every visit.
