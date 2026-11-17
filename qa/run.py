"""Single entry point for the independent QA harness.

Usage::

    PYTHONPATH=src python3 -m qa.run              # full suite
    PYTHONPATH=src python3 -m qa.run --quick      # skip mutation testing
    PYTHONPATH=src python3 -m qa.run --json out.json

Exit code is ``0`` only when every check passes and the mutation score is 1.0.
The machine-readable report is written to ``qa/reports/summary.json`` and, if
requested, to a caller-supplied path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Support both ``python3 -m qa.run`` and ``python3 qa/run.py``.
_QA_ROOT = Path(__file__).resolve().parent
if str(_QA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QA_ROOT))

from harness.core import Harness, REPO_ROOT, SRC  # noqa: E402
from harness.faults import (  # noqa: E402
    cli_exit_codes,
    fault_stage_status,
    inject_faults,
    sweep_attachment_and_top,
    sweep_max_amount,
    sweep_rounding,
)
from harness.fuzz import fuzz  # noqa: E402
from harness.layers import LAYER_CHECKS  # noqa: E402
from harness.mutation import run_mutation_suite, summary as mutation_summary  # noqa: E402
from harness.scenarios import SCENARIOS  # noqa: E402

REPORTS = REPO_ROOT / "qa" / "reports"


def run_suites(
    *,
    quick: bool = False,
    fuzz_iterations: int = 400,
    fuzz_seed: int = 20261117,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"suites": {}, "ok": True}

    scenarios = Harness()
    for scenario in SCENARIOS:
        scenario(scenarios)
    report["suites"]["scenarios"] = scenarios.summary()

    layers = Harness()
    for layer_check in LAYER_CHECKS:
        layer_check(layers)
    report["suites"]["layers"] = layers.summary()

    boundaries = Harness()
    sweep_attachment_and_top(boundaries)
    sweep_rounding(boundaries)
    sweep_max_amount(boundaries)
    report["suites"]["boundaries"] = boundaries.summary()

    faults = Harness()
    inject_faults(faults)
    cli_exit_codes(faults)
    fault_stage_status(faults)
    report["suites"]["faults"] = faults.summary()

    fuzz_harness, failures = fuzz(iterations=fuzz_iterations, seed=fuzz_seed, write_reports=True)
    fuzz_summary = fuzz_harness.summary()
    fuzz_summary["counterexamples"] = [
        {"name": failure.name, "payload": failure.payload, "detail": failure.detail}
        for failure in failures
    ]
    report["suites"]["fuzz"] = fuzz_summary
    report["fuzz_seed"] = fuzz_seed
    report["fuzz_iterations"] = fuzz_iterations

    if not quick:
        mutation_harness = Harness()
        outcomes = run_mutation_suite(mutation_harness)
        mutation = mutation_summary(outcomes)
        mutation["checks"] = mutation_harness.summary()
        mutation["outcomes"] = [
            {
                "id": outcome.mutant_id,
                "status": outcome.status,
                "description": outcome.description,
            }
            for outcome in outcomes
        ]
        report["suites"]["mutation"] = mutation
        report["ok"] = report["ok"] and mutation["mutation_score"] == 1.0
    else:
        report["suites"]["mutation"] = {"skipped": True}

    for name, suite in report["suites"].items():
        if suite.get("skipped"):
            continue
        failed = suite.get("failed", 0)
        if name == "mutation":
            failed = 0 if suite.get("mutation_score") == 1.0 else 1
        report["ok"] = report["ok"] and failed == 0

    report["totals"] = {
        "checks": sum(
            suite.get("total", 0)
            for name, suite in report["suites"].items()
            if name != "mutation" and not suite.get("skipped")
        ),
        "failed": sum(
            suite.get("failed", 0)
            for name, suite in report["suites"].items()
            if name != "mutation" and not suite.get("skipped")
        ),
    }
    return report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip mutation testing")
    parser.add_argument("--fuzz-iterations", type=int, default=400)
    parser.add_argument("--fuzz-seed", type=int, default=20261117)
    parser.add_argument("--json", type=Path, default=None, help="write the full report here")
    args = parser.parse_args(argv)

    report = run_suites(
        quick=args.quick,
        fuzz_iterations=args.fuzz_iterations,
        fuzz_seed=args.fuzz_seed,
    )

    REPORTS.mkdir(parents=True, exist_ok=True)
    summary_path = REPORTS / "summary.json"
    summary_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))

    for name, suite in report["suites"].items():
        if suite.get("skipped"):
            print(f"{name:12} skipped")
        elif name == "mutation":
            print(
                f"{name:12} killed {suite['killed']}/{suite['total']} "
                f"(score {suite['mutation_score']})"
            )
        else:
            print(f"{name:12} {suite['passed']}/{suite['total']} passed, {suite['failed']} failed")
            for failure in suite.get("failures", [])[:10]:
                print(f"  FAIL {failure['name']} :: {failure['detail'][:200]}")
    print(f"\n{'PASS' if report['ok'] else 'FAIL'} -> {summary_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
