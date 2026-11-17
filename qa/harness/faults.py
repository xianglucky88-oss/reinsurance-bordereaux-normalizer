"""Fault injection and boundary sweeps.

Two independent concerns:

* **Boundary sweeps** exercise the exact boundaries of the specification --
  attachment, layer top, rounding halves, and the maximum supported amount.
* **Fault injection** proves the pipeline fails closed.  A fault at any
  checkpoint must raise, never be swallowed, and never produce ``status: ok``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from .core import (
    Harness,
    assert_invariants,
    assert_matches_reference,
    allocations_by_key,
    run_cli,
    run_pipeline,
)

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def sweep_attachment_and_top(harness: Harness) -> None:
    """Every cent around attachment and top-of-layer must be exact."""
    treaties = [{"name": "L1", "attachment": 100, "limit": 50}]
    label = "sweep-boundaries"
    for offset in range(-3, 4):
        amount_cents = 10000 + offset
        text = f"{amount_cents // 100}.{amount_cents % 100:02d}"
        case = {"losses": [{"claim": "C", "movement": "M", "amount": text, "currency": "USD"}], "treaties": treaties}
        result = run_pipeline(case)
        expected = min(max(amount_cents - 10000, 0), 5000)
        harness.equal(f"{label}: attachment {text}", expected, sum(allocations_by_key(result).values()))
        assert_matches_reference(harness, case, result, f"{label}:attachment:{text}")
        assert_invariants(harness, case, result, f"{label}:attachment:{text}")

    for offset in range(-3, 4):
        amount_cents = 15000 + offset
        text = f"{amount_cents // 100}.{amount_cents % 100:02d}"
        case = {"losses": [{"claim": "C", "movement": "M", "amount": text, "currency": "USD"}], "treaties": treaties}
        result = run_pipeline(case)
        expected = min(max(amount_cents - 10000, 0), 5000)
        harness.equal(f"{label}: top {text}", expected, sum(allocations_by_key(result).values()))
        assert_matches_reference(harness, case, result, f"{label}:top:{text}")
        assert_invariants(harness, case, result, f"{label}:top:{text}")


def sweep_rounding(harness: Harness) -> None:
    """Half-up rounding across a contiguous cent boundary."""
    treaties = [{"name": "L1", "attachment": 0, "limit": 1000}]
    label = "sweep-rounding"
    # Third decimal 0..9 at x.x0y for positive and negative values.
    for whole in (0, 1, 2):
        for tenths in (0, 4, 5, 9):
            for third in range(10):
                text = f"{whole}.{tenths}0{third}"
                case = {"losses": [{"claim": "C", "movement": "M", "amount": text, "currency": "USD"}], "treaties": treaties}
                result = run_pipeline(case)
                # Half-up: >= 5 in the third decimal rounds up.
                base = whole * 100 + tenths * 10
                expected = base + (1 if third >= 5 else 0)
                harness.equal(f"{label}: +{text}", expected, result["claims"][0]["cumulative_cents"])
                assert_matches_reference(harness, case, result, f"{label}:+{text}")

                negative = {"losses": [{"claim": "C", "movement": "M", "amount": "-" + text, "currency": "USD"}], "treaties": treaties}
                neg_result = run_pipeline(negative)
                harness.equal(
                    f"{label}: -{text}", -expected, neg_result["claims"][0]["cumulative_cents"]
                )
                assert_matches_reference(harness, negative, neg_result, f"{label}:-{text}")


def sweep_max_amount(harness: Harness) -> None:
    """The largest supported amount is accepted; one cent more is rejected."""
    label = "sweep-max"
    at_max = {
        "losses": [{"claim": "C", "movement": "M", "amount": "1000000000000.00", "currency": "USD"}],
        "treaties": [{"name": "L1", "attachment": 0, "limit": "1000000000000.00"}],
    }
    result = run_pipeline(at_max)
    harness.equal(f"{label}: max accepted", "ok", result["status"])
    assert_matches_reference(harness, at_max, result, f"{label}:max")

    over = {
        "losses": [{"claim": "C", "movement": "M", "amount": "1000000000000.01", "currency": "USD"}],
        "treaties": [],
    }
    over_result = run_pipeline(over)
    harness.equal(f"{label}: over max rejected", "partial", over_result["status"])
    assert_matches_reference(harness, over, over_result, f"{label}:over")


def inject_faults(harness: Harness) -> None:
    """A fault at any stage must propagate, never yield ``ok``."""
    payload = {
        "losses": [{"claim": "C1", "movement": "M1", "amount": "100.00", "currency": "USD"}],
        "treaties": [{"name": "L1", "attachment": 0, "limit": "1000"}],
    }
    label = "fault-injection"
    baseline = run_pipeline(payload)
    harness.equal(f"{label}: baseline ok", "ok", baseline["status"])

    for stage in ("parse", "engine", "report"):
        raised = False
        try:
            run_pipeline(payload, fault=stage)
        except Exception as exc:  # noqa: BLE001 - the fault must propagate
            raised = True
            harness.check(
                f"{label}: {stage} fault names stage",
                stage in str(exc),
                f"unexpected exception text: {exc}",
            )
        harness.check(f"{label}: {stage} fault raises", raised, "fault was swallowed")

    # A hook can observe every checkpoint in order without raising.
    seen: List[str] = []
    run_pipeline(payload, fault_hook=seen.append)
    harness.equal(f"{label}: checkpoints observed", ["parse", "engine", "report"], seen)

    # The CLI must never report success when the input is unreadable.
    missing = REPORTS / "surely-missing-input.json"
    try:
        missing.unlink()
    except FileNotFoundError:
        pass
    exit_code, envelope = run_cli({"losses": [], "treaties": []}, REPORTS / "cli-ok.json")
    harness.equal(f"{label}: cli ok exit", 0, exit_code)

    import subprocess
    import sys

    from .core import SRC, REPO_ROOT

    completed = subprocess.run(
        [sys.executable, "-m", "reinsurance_bordereaux_normalizer.cli", str(missing)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )
    harness.equal(f"{label}: cli missing file exit", 3, completed.returncode)
    try:
        parsed = json.loads(completed.stdout)
        harness.equal(f"{label}: cli missing file status", "failed", parsed.get("status"))
        harness.check(
            f"{label}: cli missing file is machine-readable",
            parsed.get("fatal_errors", [{}])[0].get("code") == "INVALID_JSON",
            repr(parsed),
        )
    except json.JSONDecodeError:
        harness.check(f"{label}: cli missing file emits JSON", False, completed.stdout[:200])


def cli_exit_codes(harness: Harness) -> None:
    """Exit codes must follow the documented status mapping."""
    label = "cli-exit-codes"
    cases = [
        ({"losses": [], "treaties": []}, 0, "ok"),
        (
            {
                "losses": [
                    {"claim": "C", "movement": "M", "amount": "10", "currency": "USD"},
                    {"claim": "C", "movement": "M2", "amount": "bad", "currency": "USD"},
                ],
                "treaties": [],
            },
            1,
            "partial",
        ),
        (
            {
                "losses": [],
                "treaties": [
                    {"name": "L1", "attachment": 0, "limit": 100},
                    {"name": "L2", "attachment": 50, "limit": 100},
                ],
            },
            2,
            "failed",
        ),
    ]
    for index, (payload, expected_code, expected_status) in enumerate(cases):
        exit_code, envelope = run_cli(payload, REPORTS / f"cli-case-{index}.json")
        harness.equal(f"{label}: case {index} exit", expected_code, exit_code)
        harness.equal(f"{label}: case {index} status", expected_status, envelope.get("status"))


def fault_stage_status(harness: Harness) -> None:
    """A fault-injected run must never be served as ``status: ok`` by callers."""
    label = "fault-status"
    payload = {
        "losses": [{"claim": "C1", "movement": "M1", "amount": "1.00", "currency": "USD"}],
        "treaties": [],
    }
    raised = 0
    for stage in ("parse", "engine", "report"):
        try:
            run_pipeline(payload, fault=stage)
        except Exception:
            raised += 1
    harness.equal(f"{label}: every stage fails closed", 3, raised)
