# Independent QA harness

This directory is a **second, independent implementation and test suite** for
the bordereaux normalizer. It exists to prove the settlement semantics are
auditable, not to restate the implementation.

Nothing in `qa/reference/` imports the production package. The reference model
parses decimal literals itself and computes in `fractions.Fraction`, so a
shared rounding bug cannot hide behind a shared dependency.

## Run

```bash
# Fast: everything except mutation testing
PYTHONPATH=src python3 -m qa.run --quick

# Full: includes mutation testing (slower; copies src/ per mutant)
PYTHONPATH=src python3 -m qa.run

# Deterministic fuzz campaign controls
PYTHONPATH=src python3 -m qa.run --quick --fuzz-iterations 1000 --fuzz-seed 7
```

Exit code is `0` only when every suite passes **and** the mutation score is
`1.0`. A machine-readable report is written to `qa/reports/summary.json`.

## Layout

| Path | Purpose |
| --- | --- |
| `reference/reference_model.py` | Independent spec implementation in `Fraction`; no production imports. |
| `harness/core.py` | Production driver, invariants, reference comparison, permutation/replay checks. |
| `harness/scenarios.py` | Named scenarios for every settlement semantic. |
| `harness/layers.py` | Direct parser / engine / report checks (no pipeline in between). |
| `harness/faults.py` | Boundary sweeps, rounding sweeps, fault injection, CLI exit codes. |
| `harness/fuzz.py` | Seeded generator + shrinker for minimal counterexamples. |
| `harness/mutation.py` | Mutation testing: 21 semantic mutants, all must be killed. |
| `reports/summary.json` | Latest machine-readable result. |

## What is verified

**Normalization and movement semantics**

* increases and decreases both move the claim's cumulative loss;
* negative movements are *signed adjustments*, never new positive loss;
* a movement replayed byte-for-byte counts once and is recorded as a replay;
* a key replayed with a different amount or currency is excluded as a conflict
  (never silently double-counted and never silently dropped);
* amounts are rounded half-up to cents; `-0.00` canonicalises to `0.00`;
* invalid, non-finite, quoted, boolean, empty and over-range amounts are
  rejected at row level and never become zero loss.

**Treaty layering**

* coverage is `clamp(position - attachment, 0, limit)`; the attachment point
  itself is uncovered and the top of the layer is fully covered;
* a loss straddling two layers is split exactly at the boundary;
* a loss above the tower is unallocated and never allocated past the limit;
* a gap between layers stays unallocated;
* each claim has its own tower — per-claim layer totals never exceed the limit;
* currency-scoped layers only absorb movements in their currency.

**Currency**

* there is no FX and no external rate service;
* cumulative loss, allocations and totals are tracked per `(claim, currency)`;
* mixed-currency claims are warned about, not silently netted.

**Conservation and traceability**

* `net movements = allocations + unallocated`, per currency, in integer cents;
* per-layer allocations telescope to final coverage;
* claim cumulatives match the movement ledger recomputed independently;
* every allocation row carries claim, movement, currency, treaty and source row
  indices, and every source index is validated against the input.

**Input reordering**

* permuting `losses` leaves every conserved quantity and every per-movement
  attribution identical (attribution follows canonical `(claim, currency,
  movement)` order, not input order).

**Failure behaviour**

* row-level errors yield `partial` and still process valid rows;
* fatal configuration errors (overlap, duplicate names, bad attachment/limit)
  yield `failed`, allocate nothing, and mark conservation as *not evaluated*;
* injected faults at `parse`, `engine` and `report` propagate — they are never
  swallowed into an `ok` report;
* CLI exit codes: `0` ok, `1` partial, `2` failed, `3` unreadable input, and the
  CLI always emits a machine-readable envelope.

## Shrinking

When fuzzing finds a failure, `harness/fuzz.py` greedily deletes rows and
simplifies identifiers while the *same named check* still fails, then writes the
minimal counterexample to `qa/reports/counterexample-<n>.json`.

## Mutation testing

`harness/mutation.py` copies `src/` to a scratch directory, applies one textual
semantic edit, and re-runs the suite against the copy with `RBN_SRC` pointing at
it. The working tree is never modified. Mutants cover rounding mode, sign
handling, dedupe, currency scoping, clamping, incremental allocation, conflict
detection, range checks, overlap detection, canonical ordering, per-claim limits,
status mapping, trace provenance, fatal handling and fault propagation.
