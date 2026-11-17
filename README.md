# Reinsurance Bordereaux Normalizer

Normalize broker bordereaux (CSV/JSON) into confirmed claim movements and
recompute treaty-layer settlement with full traceability: per-period
movements, cumulative attachment consumption, per-layer allocation,
recoveries, reinstatement premium, and unallocated amounts — replayable under
any treaty version, with totals that always reconcile.

## Processing boundaries

| Boundary | What it is | Guarantee |
|---|---|---|
| Event-level increment | Every confirmed movement is appended to an immutable event log with its FX conversion **frozen on the event** (snapshot id, revision, rate, 2dp/6dp precision) | The event log is the only source of truth |
| Monthly snapshot | `close_all` derives one close per booking period from all events up to it; empty months are closed with zero movements | A closed period is never mutated; late movements land in the period they are booked in |
| Full rebuild | `rebuild` replays the whole event log and compares every stored close byte-for-byte | `consistent: true` is the audit-consistency proof after any rerun |

A close is a pure function of `(events, reference data, period, previous
close)`, so incremental closes and a full rebuild cannot diverge.

## Movement semantics (not simple addition)

A claim's ground-up position is the sum of signed event **contributions**,
computed at confirmation time:

- `ADDITION` — adds `amount`, converted at the booking period's FX snapshot.
- `CORRECTION` — supersedes the target addition's amount; contributes only
  the *delta* (`new − previous effective`), converted at the correction's own
  frozen rate. Correcting last month's movement produces a negative/positive
  delta in this month's movement; last month's close is untouched.
- `WITHDRAWAL` — reverses the target's frozen settlement contributions
  exactly (no FX drift on reversal).
- `RECOVERY` — reduces the ground-up loss and is reported separately as
  recovery; it flows back through the layers and reduces reinstatement
  premium.

Idempotency: a resubmitted file is a `duplicate` batch (content hash); a
resubmitted `movement_id` with identical content is skipped; the same id with
different content is a conflict and must be sent as a `CORRECTION`.

## Treaty layers

Each treaty version defines ordered, non-overlapping layers
(`attachment`, `limit`, `reinstatements`, `reinstatement_premium_rate`).
Per claim, a layer cedes `min(max(cum − attachment, 0), limit)`. Per layer,
the program aggregate is capped at `limit × (1 + reinstatements)`; when the
cap binds, capacity is distributed to claims in first-reported order and the
shortfall is reported per claim as `aggregate_shortfall` /
`unallocated.above_aggregate`. Aggregate usage beyond the first `limit`
consumes reinstatements and accrues premium pro-rata
(`rate × reinstated amount`).

Unallocated amounts reconcile exactly per claim:
`retention + gap + above_layer + above_aggregate + no_cover + ceded = ground_up`.

## Treaty versions and replay

Versions have non-overlapping effective ranges. Registered versions are
immutable; an open version may be closed off and a new one appended only if
no **closed** period's version mapping changes — a version change can only
govern its own effective range. `replay --period P [--program X
--treaty-version V]` recomputes any closed period under a pinned version
without persisting anything.

## Bad rows and partial batches

Every row is validated independently. Bad rows are quarantined with row
number and reason; good rows in the same batch still confirm (`partial`).
A malformed file rejects only its own batch. A batch for an already-closed
period is rejected and must be rebooked in an open period.

## Modules

- `models.py`: immutable value types, decimal money (2dp) and rates (6dp), period arithmetic.
- `parser.py`: JSON/CSV extraction and row-level validation (pure).
- `engine.py`: domain calculation only — positions, layer allocation, closes.
- `store.py`: event log, reference data, snapshots; deterministic JSON persistence.
- `pipeline.py`: orchestration boundary (register/ingest/close/rebuild/replay/report).
- `report.py`: stable output envelope.
- `cli.py`: deterministic JSON CLI.

## Run

```bash
# end-to-end demo bundle (registers reference data, ingests 5 batches,
# closes 2026-01..2026-04, verifies a full rebuild)
PYTHONPATH=src python3 -m reinsurance_bordereaux_normalizer.cli

# persistent store workflows
PYTHONPATH=src python3 -m reinsurance_bordereaux_normalizer.cli process --store ./s fixtures/reference.json fixtures/batches/broker_b__BB-2026-03__2026-03.csv
PYTHONPATH=src python3 -m reinsurance_bordereaux_normalizer.cli rebuild --store ./s
PYTHONPATH=src python3 -m reinsurance_bordereaux_normalizer.cli replay --store ./s --period 2026-03 --program PROP-XOL --treaty-version 2
PYTHONPATH=src python3 -m reinsurance_bordereaux_normalizer.cli report --store ./s --period 2026-03

PYTHONPATH=src python3 -m unittest discover -s tests -v
```

CSV files are named `<source>__<batch_id>__<period>.csv`; rows may carry
`risk_id`/`treaty_program` to register the risk and claim implicitly, and a
correction may appear before its target inside one file.

The fixture is local and deterministic; no network or credentials are required.
