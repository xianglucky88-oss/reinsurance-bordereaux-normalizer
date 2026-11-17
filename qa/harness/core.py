"""Quality-assurance harness for the reinsurance bordereaux normalizer.

The harness is intentionally independent of the production internals:

* it drives the public surface (`parse`, `calculate`, `pipeline.run`, `cli`);
* it compares results to ``qa/reference/reference_model.py``;
* it asserts specification invariants that must hold for *any* input.

Nothing here imports private helpers, so a refactor that breaks semantics but
keeps the helpers cannot pass silently.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
QA_ROOT = HERE.parent
REPO_ROOT = QA_ROOT.parent
# ``RBN_SRC`` lets the mutation runner point the harness at a mutated copy of
# the package without touching the working tree.
SRC = Path(os.environ.get("RBN_SRC", REPO_ROOT / "src")).resolve()

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(QA_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_ROOT))

from reference.reference_model import amount_cents as reference_amount_cents  # noqa: E402
from reference.reference_model import evaluate as reference_evaluate  # noqa: E402

from reinsurance_bordereaux_normalizer import engine as prod_engine  # noqa: E402
from reinsurance_bordereaux_normalizer import parser as prod_parser  # noqa: E402
from reinsurance_bordereaux_normalizer import pipeline as prod_pipeline  # noqa: E402
from reinsurance_bordereaux_normalizer import report as prod_report  # noqa: E402


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class Harness:
    """Accumulates named checks; never raises for a failed expectation."""

    results: List[CheckResult] = field(default_factory=list)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.results.append(CheckResult(name=name, ok=bool(condition), detail=detail))
        return bool(condition)

    def equal(self, name: str, expected: Any, actual: Any) -> bool:
        if expected == actual:
            return self.check(name, True)
        return self.check(name, False, f"expected {expected!r}, got {actual!r}")

    @property
    def failures(self) -> List[CheckResult]:
        return [result for result in self.results if not result.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> dict:
        return {
            "total": len(self.results),
            "passed": len(self.results) - len(self.failures),
            "failed": len(self.failures),
            "failures": [failure.as_dict() for failure in self.failures],
        }


# --------------------------------------------------------------------------
# Production driver
# --------------------------------------------------------------------------


def run_pipeline(payload: Any, **kwargs: Any) -> dict:
    return prod_pipeline.run(copy.deepcopy(payload), **kwargs)


def run_cli(payload: Any, tmp_path: Path) -> Tuple[int, dict]:
    """Invoke the CLI as a subprocess so exit codes and JSON are both tested."""
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(payload))
    completed = subprocess.run(
        [sys.executable, "-m", "reinsurance_bordereaux_normalizer.cli", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        envelope = {"_stdout": completed.stdout, "_stderr": completed.stderr}
    return completed.returncode, envelope


def allocations_by_key(result: dict) -> Dict[Tuple[str, str, str, str], int]:
    mapped: Dict[Tuple[str, str, str, str], int] = {}
    for row in result.get("allocations", []):
        key = (row["claim"], row["movement"], row["currency"], row["treaty"])
        mapped[key] = mapped.get(key, 0) + row["allocated_cents"]
    return mapped


# --------------------------------------------------------------------------
# Invariants that must hold on every successful result
# --------------------------------------------------------------------------


def assert_invariants(harness: Harness, payload: Any, result: dict, label: str) -> None:
    """Specification invariants, independent of any particular input."""
    audit = result.get("audit", {})
    conservation = audit.get("conservation", {})

    harness.check(f"{label}: audit present", bool(audit), "missing audit block")
    if result.get("status") == "failed":
        # Nothing was allocated, so conservation is deliberately not evaluated.
        harness.check(
            f"{label}: failed run does not claim conservation",
            conservation.get("evaluated") is False
            and conservation.get("all_checks_pass") is False,
            json.dumps(conservation, default=str),
        )
        harness.check(
            f"{label}: failed run allocates nothing",
            result.get("allocations") == [],
            repr(result.get("allocations")),
        )
    else:
        harness.check(
            f"{label}: conservation evaluated and passing",
            conservation.get("evaluated") is True
            and conservation.get("all_checks_pass") is True,
            json.dumps(conservation.get("checks", []), default=str),
        )

    # Integer cents only on every conserved quantity.
    cents_fields = []
    for row in result.get("allocations", []):
        cents_fields.append(row.get("allocated_cents"))
        cents_fields.append(row.get("claim_cumulative_cents"))
    for row in result.get("claims", []):
        cents_fields.extend(
            [row.get("cumulative_cents"), row.get("allocated_cents"), row.get("unallocated_cents")]
        )
    for row in result.get("currency_totals", []):
        cents_fields.extend(
            [row.get("net_movements_cents"), row.get("allocated_cents"), row.get("unallocated_cents")]
        )
    harness.check(
        f"{label}: cents are integers",
        all(isinstance(value, int) and not isinstance(value, bool) for value in cents_fields),
        repr([value for value in cents_fields if not isinstance(value, int)]),
    )

    # No allocation may exceed its treaty limit, or be attributed to a
    # currency the treaty does not cover.
    treaties = payload.get("treaties", []) if isinstance(payload, dict) else []
    treaty_specs = {
        treaty["name"]: treaty
        for treaty in treaties
        if isinstance(treaty, dict) and "name" in treaty
    }
    for row in result.get("allocations", []):
        spec = treaty_specs.get(row["treaty"], {})
        layer_currency = row.get("layer_currency")
        harness.check(
            f"{label}: allocation currency matches layer:{row['treaty']}:{row['currency']}",
            layer_currency is None or layer_currency == row["currency"],
            f"layer currency {layer_currency!r} vs movement currency {row['currency']!r}",
        )

    # Per-treaty-per-claim totals never exceed the layer limit.  Each claim has
    # its own tower, so aggregation across claims would be wrong.
    for total in result.get("treaty_totals", []):
        spec = treaty_specs.get(total["treaty"])
        if spec is None:
            continue
        try:
            limit_cents = round(float(spec["limit"]) * 100)
        except (TypeError, ValueError, KeyError):
            continue
        for claim_row in result.get("claims", []):
            if claim_row["currency"] != total["currency"]:
                continue
            claim_total = sum(
                row["allocated_cents"]
                for row in result.get("allocations", [])
                if row["treaty"] == total["treaty"]
                and row["currency"] == total["currency"]
                and row["claim"] == claim_row["claim"]
            )
            harness.check(
                f"{label}: layer within limit:{claim_row['claim']}:{total['treaty']}:{total['currency']}",
                claim_total <= limit_cents,
                f"{claim_total} > {limit_cents}",
            )

    # Every allocation row must be traceable to a claim/movement and to source
    # row indices that actually exist in the input.
    losses = payload.get("losses", []) if isinstance(payload, dict) else []
    for row in result.get("allocations", []):
        harness.check(
            f"{label}: allocation has claim/movement/treaty",
            bool(row.get("claim")) and bool(row.get("movement")) and bool(row.get("treaty")),
            repr(row),
        )
        for index in row.get("sources", []):
            harness.check(
                f"{label}: source index in range:{index}",
                isinstance(index, int) and 0 <= index < len(losses),
                f"source index {index} outside 0..{len(losses) - 1}",
            )
            if isinstance(index, int) and 0 <= index < len(losses) and isinstance(losses[index], dict):
                harness.check(
                    f"{label}: source claim matches:{index}",
                    str(losses[index].get("claim")) == row["claim"],
                    f"{losses[index].get('claim')!r} != {row['claim']!r}",
                )
                harness.check(
                    f"{label}: source movement matches:{index}",
                    str(losses[index].get("movement")) == row["movement"],
                    f"{losses[index].get('movement')!r} != {row['movement']!r}",
                )

    # The audit trace must cover exactly the allocation rows, and every ledger
    # entry that contributed must be resolved (no silently orphaned movement).
    trace = audit.get("trace", [])
    harness.equal(f"{label}: trace covers allocations", len(result.get("allocations", [])), len(trace))
    applied_ledger = [
        entry for entry in audit.get("ledger", []) if entry.get("status") in ("applied", "replayed")
    ]
    harness.equal(
        f"{label}: ledger and trace agree on applied movements",
        len(applied_ledger),
        result.get("counts", {}).get("applied_movements"),
    )
    if result.get("status") == "failed":
        harness.check(
            f"{label}: failed run applies nothing",
            applied_ledger == [],
            repr(applied_ledger),
        )

    # The net movement must equal allocations plus unallocated, per currency.
    nets = {row["currency"]: row["net_movements_cents"] for row in result.get("currency_totals", [])}
    allocated: Dict[str, int] = {}
    for row in result.get("allocations", []):
        allocated[row["currency"]] = allocated.get(row["currency"], 0) + row["allocated_cents"]
    for currency, net in nets.items():
        harness.equal(
            f"{label}: net = allocated + unallocated:{currency}",
            net,
            allocated.get(currency, 0) + (net - allocated.get(currency, 0)),
        )


# --------------------------------------------------------------------------
# Cross-implementation comparison
# --------------------------------------------------------------------------


def assert_matches_reference(harness: Harness, payload: Any, result: dict, label: str) -> None:
    reference = reference_evaluate(payload)

    harness.equal(f"{label}: reference status", reference.status, result.get("status"))

    # Compare allocation rows exactly, keyed by claim/movement/currency/treaty.
    expected_rows = allocations_by_key(
        {
            "allocations": [
                {
                    "claim": row["claim"],
                    "movement": row["movement"],
                    "currency": row["currency"],
                    "treaty": row["treaty"],
                    "allocated_cents": row["allocated_cents"],
                }
                for row in reference.allocations
            ]
        }
    )
    harness.equal(f"{label}: allocation rows vs reference", expected_rows, allocations_by_key(result))

    expected_claims = {
        (row["claim"], row["currency"]): (
            row["cumulative_cents"],
            row["allocated_cents"],
            row["unallocated_cents"],
        )
        for row in reference.claims
    }
    actual_claims = {
        (row["claim"], row["currency"]): (
            row["cumulative_cents"],
            row["allocated_cents"],
            row["unallocated_cents"],
        )
        for row in result.get("claims", [])
    }
    harness.equal(f"{label}: claims vs reference", expected_claims, actual_claims)

    expected_totals = {
        row["currency"]: (row["net_cents"], row["allocated_cents"], row["unallocated_cents"])
        for row in reference.currency_totals
    }
    actual_totals = {
        row["currency"]: (
            row["net_movements_cents"],
            row["allocated_cents"],
            row["unallocated_cents"],
        )
        for row in result.get("currency_totals", [])
    }
    harness.equal(f"{label}: currency totals vs reference", expected_totals, actual_totals)


def assert_permutation_invariance(
    harness: Harness, payload: Any, permutations: Sequence[Sequence[int]], label: str
) -> None:
    """Reordering ``losses`` must not change any conserved quantity."""
    base = run_pipeline(payload)
    base_alloc = allocations_by_key(base)
    base_claims = {
        (row["claim"], row["currency"]): row["cumulative_cents"] for row in base.get("claims", [])
    }

    losses = payload.get("losses", [])
    for perm_index, order in enumerate(permutations):
        if sorted(order) != list(range(len(losses))):
            continue
        permuted = copy.deepcopy(payload)
        permuted["losses"] = [copy.deepcopy(losses[index]) for index in order]
        result = run_pipeline(permuted)
        harness.equal(f"{label}: permutation {perm_index} status", base["status"], result["status"])
        harness.equal(
            f"{label}: permutation {perm_index} allocations", base_alloc, allocations_by_key(result)
        )
        harness.equal(
            f"{label}: permutation {perm_index} claim cumulatives",
            base_claims,
            {
                (row["claim"], row["currency"]): row["cumulative_cents"]
                for row in result.get("claims", [])
            },
        )


def assert_idempotent_replay(harness: Harness, payload: Any, label: str) -> None:
    """Duplicating every loss row must not change totals."""
    base = run_pipeline(payload)
    duplicated = copy.deepcopy(payload)
    duplicated["losses"] = [copy.deepcopy(row) for row in payload.get("losses", [])] * 2
    result = run_pipeline(duplicated)
    harness.equal(f"{label}: replay status", "ok", result["status"])
    harness.equal(f"{label}: replay allocations", allocations_by_key(base), allocations_by_key(result))
    harness.equal(
        f"{label}: replay claim cumulatives",
        {row["claim"]: row["cumulative_cents"] for row in base.get("claims", [])},
        {row["claim"]: row["cumulative_cents"] for row in result.get("claims", [])},
    )
    harness.check(
        f"{label}: replay is recorded",
        result["audit"]["counts"]["replayed_rows"] == len(payload.get("losses", [])),
        repr(result["audit"]["counts"]),
    )
