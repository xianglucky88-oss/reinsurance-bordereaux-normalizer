# Reinsurance Bordereaux Normalizer

Normalize loss movements and allocate them across treaty layers, with an
auditable, integer-cent reconciliation for every run.

## Modules

- `src/reinsurance_bordereaux_normalizer/models.py`: immutable result and domain contracts.
- `src/reinsurance_bordereaux_normalizer/parser.py`: field validation, exact cent normalization, replay/conflict resolution, audit ledger.
- `src/reinsurance_bordereaux_normalizer/engine.py`: domain calculation only (integer cents, per-claim towers, signed movements).
- `src/reinsurance_bordereaux_normalizer/report.py`: stable output envelope and independent conservation audit.
- `src/reinsurance_bordereaux_normalizer/pipeline.py`: orchestration boundary and failure contract.
- `src/reinsurance_bordereaux_normalizer/cli.py`: deterministic JSON CLI with documented exit codes.

## Run

```bash
PYTHONPATH=src python -m reinsurance_bordereaux_normalizer.cli fixtures/input.json
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m qa.run --quick     # independent QA harness
PYTHONPATH=src python3 -m qa.run             # + mutation testing
```

The fixture is local and deterministic; no network, credentials, or FX service
is required.

## Settlement semantics

These are the rules the QA harness holds the implementation to
(`qa/README.md` has the full matrix).

**Normalization.** Amounts are parsed as decimal text, must be finite, and are
rounded half-up (away from zero) to two decimal places. `-0.00` canonicalises to
`0.00`. Invalid, non-finite, quoted, boolean, empty, or over-range amounts are
rejected at row level — they never become zero loss.

**Movements.** A movement key is `(claim, movement)`. A byte-for-byte replay of
that key counts once and is recorded as a replay. A key reported twice with
different normalized `(amount, currency)` is a conflict: the whole key is
excluded and the run is `partial`. Negative movements are signed downward
adjustments; they reduce the claim's cumulative loss and can claw back an
earlier allocation. A negative movement is never treated as fresh positive loss.

**Currency.** There is no FX conversion. Cumulative loss, allocations, and
totals are tracked per `(claim, currency)`. A treaty may declare a `currency`,
in which case it only applies to movements in that currency; a treaty without
one applies to every currency. A claim spanning multiple currencies is warned
about, never silently netted.

**Layering.** Each claim has its own tower; per-claim layer totals never exceed
the layer limit. Coverage is `clamp(position - attachment, 0, limit)`, so the
attachment point itself is uncovered and the top of the layer is fully covered.
A loss straddling two layers splits exactly at the boundary. Loss above the
tower, below the first attachment, or inside a tower gap is unallocated.
Overlapping layers, duplicate treaty names, negative attachments, and
non-positive limits are fatal configuration errors.

**Ordering.** Movements are applied in canonical `(claim, currency, movement)`
order, so permuting `losses` does not change any conserved quantity or any
per-movement attribution. `sources` always reflect real input row positions.

**Conservation.** All money is integer cents. For each currency
`net movements = allocations + unallocated`, per-layer allocations telescope to
the final coverage of each claim's cumulative position, and every allocation
row is traceable to its claim, movement, currency, treaty, and source rows.

## Output

`run(payload)` returns a deterministic envelope:

```
status: "ok" | "partial" | "failed"
errors / warnings / fatal_errors: stable upper-case codes with source indices
allocations[]: claim, movement, currency, treaty, allocated(_cents),
               claim_cumulative(_cents), sources
claims[]: claim, currency, cumulative/allocated/unallocated (cents)
currency_totals[], treaty_totals[], replay_skips[], counts{}
audit{}: ledger (one entry per movement key, incl. replays and rejections),
         trace (one entry per allocation row), conservation (per-currency
         reconciliation, layer limits, telescoping, and traceability checks)
```

`status` is `partial` when rows were rejected but valid rows were processed,
and `failed` when a fatal configuration error prevented any allocation. On
`failed`, conservation is marked `evaluated: false` rather than claiming a
reconciliation that never ran.

CLI exit codes: `0` ok, `1` partial, `2` failed, `3` unreadable or invalid JSON
input. The CLI always prints a machine-readable envelope.

## Independent QA harness

`qa/` contains a second implementation of this specification (in
`fractions.Fraction`, with no production imports) plus scenario, layer,
boundary, fault-injection, fuzz-with-shrinking, and mutation-testing suites.
See [`qa/README.md`](qa/README.md).
