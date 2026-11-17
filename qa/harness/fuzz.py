"""Deterministic property-based fuzzing, with minimal-counterexample shrinking.

The generator produces payloads that stress settlement semantics: multiple
claims, negative movements, replays, conflicts, boundary attachments, rounding
halves, mixed currencies, and invalid rows.  Every generated payload is checked
against the reference model and the specification invariants.

When a failure is found the payload is reduced by repeatedly deleting rows and
simplifying values while the *same named failure* still reproduces.  The minimal
counterexample is written to ``qa/reports/`` so it can become a regression
fixture.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .core import Harness, assert_invariants, assert_matches_reference, run_pipeline

REPORTS = Path(__file__).resolve().parent.parent / "reports"

CLAIMS = ["C1", "C2", "C3"]
MOVEMENTS = ["M1", "M2", "M3", "M4"]
CURRENCIES = ["USD", "EUR", "usd", "GBP"]

BOUNDARY_AMOUNTS = [
    "0.00", "0.01", "-0.01", "0.005", "-0.005", "99.99", "100.00", "100.01",
    "150.00", "150.01", "199.99", "200.00", "250.00", "250.01", "1.005",
]
RANDOM_REGIONS = [(0, 5), (90, 210), (240, 400)]


@dataclass
class Failure:
    name: str
    payload: dict
    detail: str


def _random_amount(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.45:
        low, high = rng.choice(RANDOM_REGIONS)
        return f"{rng.uniform(low, high):.2f}"
    if roll < 0.75:
        return rng.choice(BOUNDARY_AMOUNTS)
    if roll < 0.85:
        return rng.choice(["-0.00", "0", "1", 25, 25.5, "-25"])
    if roll < 0.92:
        return rng.choice(["bad", "", "1e3", "NaN", "1,000.00", None, True])
    return f"{rng.randint(-500, 500)}.{rng.randint(0, 99):02d}"


def _random_treaties(rng: random.Random) -> List[dict]:
    count = rng.randint(0, 3)
    treaties: List[dict] = []
    cursor = rng.choice([0, 50, 100, 150])
    for index in range(count):
        limit = rng.choice([25, 50, 100, 200])
        gap = rng.choice([0, 0, 25, 50])
        attachment = cursor + gap
        row: Dict[str, Any] = {
            "name": f"L{index + 1}",
            "attachment": attachment,
            "limit": limit,
        }
        if rng.random() < 0.15:
            row["currency"] = rng.choice(["USD", "EUR"])
        treaties.append(row)
        cursor = attachment + limit
    if rng.random() < 0.08:
        # Occasionally emit a deliberately broken treaty.
        bad = rng.choice(
            [
                {"name": "BAD", "attachment": -1, "limit": 10},
                {"name": "BAD", "attachment": 0, "limit": 0},
                {"name": "L1", "attachment": 0, "limit": 10},
                "not-a-treaty",
            ]
        )
        treaties.append(bad)
    return treaties


def generate_payload(rng: random.Random) -> dict:
    row_count = rng.randint(0, 8)
    losses: List[dict] = []
    for _ in range(row_count):
        roll = rng.random()
        if roll < 0.06:
            losses.append({"claim": rng.choice(CLAIMS), "movement": "M1", "currency": "USD"})
            continue
        if roll < 0.10:
            losses.append("not-a-loss")
            continue
        row = {
            "claim": rng.choice(CLAIMS),
            "movement": rng.choice(MOVEMENTS),
            "amount": _random_amount(rng),
            "currency": rng.choice(CURRENCIES),
        }
        losses.append(row)

    if losses and rng.random() < 0.35:
        # Exact replay of an existing row.
        losses.append(json.loads(json.dumps(rng.choice(losses))))

    payload: Dict[str, Any] = {"losses": losses, "treaties": _random_treaties(rng)}
    if rng.random() < 0.05:
        payload = {"losses": rng.choice([None, "x", 5]), "treaties": payload["treaties"]}
    return payload


def check_payload(harness: Harness, payload: Any, label: str) -> None:
    result = run_pipeline(payload)
    assert_invariants(harness, payload, result, label)
    assert_matches_reference(harness, payload, result, label)


def shrink(
    payload: dict,
    failing_name: str,
    reproduce: Callable[[Any], Optional[str]],
    max_steps: int = 200,
) -> Tuple[Any, str]:
    """Greedily reduce ``payload`` while ``reproduce`` keeps returning a failure.

    ``reproduce`` returns the first failing check name (or ``None``).  A
    candidate is kept only if the *same* named check still fails, which prevents
    shrinking into a different bug.
    """
    current = json.loads(json.dumps(payload, default=str))
    detail = reproduce(current) or ""

    def still_fails(candidate: Any) -> bool:
        return reproduce(candidate) == failing_name

    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False
        losses = current.get("losses", []) if isinstance(current, dict) else []
        if isinstance(losses, list):
            for index in range(len(losses)):
                candidate = json.loads(json.dumps(current, default=str))
                del candidate["losses"][index]
                steps += 1
                if still_fails(candidate):
                    current = candidate
                    improved = True
                    break
        if improved:
            continue

        # Simplify amount values to a single "0.01" and identifiers to "C"/"M".
        for index, row in enumerate(current.get("losses", []) if isinstance(current, dict) else []):
            if not isinstance(row, dict):
                continue
            for field, replacement in (("claim", "C"), ("movement", "M"), ("currency", "USD")):
                if row.get(field) == replacement:
                    continue
                candidate = json.loads(json.dumps(current, default=str))
                candidate["losses"][index][field] = replacement
                steps += 1
                if still_fails(candidate):
                    current = candidate
                    improved = True
                    break
            if improved:
                break
        if improved:
            continue

        # Simplify treaties by dropping one at a time.
        treaties = current.get("treaties", []) if isinstance(current, dict) else []
        if isinstance(treaties, list):
            for index in range(len(treaties)):
                candidate = json.loads(json.dumps(current, default=str))
                del candidate["treaties"][index]
                steps += 1
                if still_fails(candidate):
                    current = candidate
                    improved = True
                    break
    return current, detail


def _reproduce(payload: Any) -> Optional[str]:
    local = Harness()
    check_payload(local, payload, "fuzz")
    if local.failures:
        return local.failures[0].name
    return None


def fuzz(
    iterations: int = 400,
    seed: int = 20261117,
    write_reports: bool = True,
) -> Tuple[Harness, List[Failure]]:
    rng = random.Random(seed)
    harness = Harness()
    failures: List[Failure] = []

    for index in range(iterations):
        payload = generate_payload(rng)
        local = Harness()
        check_payload(local, payload, f"fuzz-{index}")
        for result in local.results:
            harness.results.append(result)
        if local.failures:
            first = local.failures[0]
            minimal, detail = shrink(payload, first.name, _reproduce)
            failures.append(Failure(name=first.name, payload=minimal, detail=detail or first.detail))

    if write_reports and failures:
        REPORTS.mkdir(parents=True, exist_ok=True)
        for index, failure in enumerate(failures):
            target = REPORTS / f"counterexample-{index}.json"
            target.write_text(json.dumps(failure.payload, indent=2, sort_keys=True))
    return harness, failures
