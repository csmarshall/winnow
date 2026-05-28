# Decision flows (Frigate adapter)

How a candidate moves through review for each Frigate model type, per the verdict
model in [ADR-0014](adr/0014-verdict-model-reassign-first-class.md). Verdicts are
local until you **commit** (ADR-0013); the "commit:" boxes are what a commit pushes.

## Which path?

```mermaid
flowchart TD
  X[Frigate candidate] --> T{Model type}
  T -->|dog / car classifier| CLF[Binary-sweep subtype pools]
  T -->|person / face recognition| FACE[Who-is-this flow]
```

## Classifier (dogs / cars) — binary-sweep

The same crop is a sibling candidate in every subtype pool. "No" leaves it for the
other pools; reassign reshuffles it (no here, yes there).

```mermaid
flowchart TD
  C["Crop in a subtype pool — 'Is this SUBTYPE?'"]
  C -->|Yes| TY["commit: categorize into dataset/SUBTYPE"]
  C -->|Reassign to another subtype| RESH["reshuffle: NO here + YES in the target pool"]
  RESH --> TT["commit: categorize into dataset/TARGET"]
  C -->|No| DEFER["not this subtype — stays pending for the sibling pools"]
  DEFER --> LEFT{Confirmed in any pool?}
  LEFT -->|"No — rejected everywhere"| NONE["leftover → none / hard-negative (ADR-0006)"]
  C -->|Skip| PARK["parked — revisit"]
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
