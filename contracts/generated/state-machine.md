# Grow stage state machine (Phase 0 deliverable)

Authoritative implementation: `mycoloop/modules/biology.py` (`FORWARD`, `LATERAL`,
`allowed_transitions`). This document is generated from the same intent; a change
to one without the other is a contract violation.

```
INOCULATED
   └─> COLONIZING
         └─> COLONIZATION_COMPLETE
               └─> PINNING_INITIATED  <──────────────┐
                     └─> PINNING_ACTIVE              │
                           └─> MATURATION            │
                                 └─> HARVEST_READY   │
                                       └─> HARVESTED │
                                             └─> FLUSH_COMPLETE
                                                   (flush += 1)

Any non-terminal stage ──> CONTAMINATED   (terminal)
Any non-terminal stage ──> ABORTED        (terminal)
```

## Rules

| Rule | Enforcement |
|---|---|
| Only the next forward stage, or a lateral terminal stage, is legal | `transition()` returns **409** with the allowed set |
| `FLUSH_COMPLETE -> PINNING_INITIATED` increments `flush` | `transition()` |
| Harvest is only legal from `MATURATION` or `HARVEST_READY` | `log_harvest()` returns **409** otherwise |
| Terminal stages have no outgoing edges | `allowed_transitions()` returns `set()` |
| Every transition appends a `stage_events` row | append-only, DB trigger blocks UPDATE/DELETE |
| Harvest adds to `yield_g`, never overwrites it | `log_harvest()` |
| Contamination sets `contaminated` and `ended_at` | `log_contamination()` |

## Why the strictness matters

Stage labels are the join key between biology and telemetry
(`telemetry_clean.stage`), and therefore the optimizer's training signal. A
silently accepted illegal transition corrupts every downstream conclusion, so
the API refuses it instead of "helpfully" writing it.

## Reconstruction query

`GET /grow-cycles/{id}/timeline` returns stage events + harvests + contamination
+ annotations in chronological order, plus `environment_by_stage` computed only
from validated telemetry. That is the Phase 5 exit criterion, executable.
