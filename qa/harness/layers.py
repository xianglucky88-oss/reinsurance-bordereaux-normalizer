"""Direct, per-layer checks.

The pipeline is not the only contract.  These checks drive ``parser``,
``engine`` and ``report`` directly so that a defect cannot hide behind a
correct-looking pipeline.  They also make the layer under test obvious when a
check fails.
"""

from __future__ import annotations

import copy
import random
from decimal import Decimal
from typing import List

from .core import Harness, prod_engine, prod_parser, prod_pipeline, prod_report, run_pipeline

from reinsurance_bordereaux_normalizer.models import Movement, Treaty


def build_treaty(name: str, attachment: str, limit: str, currency=None, index: int = 0) -> Treaty:
    return Treaty(
        name=name,
        attachment=Decimal(attachment),
        limit=Decimal(limit),
        currency=currency,
        source_index=index,
    )


def build_movement(
    claim: str, movement: str, amount: str, currency: str = "USD", index: int = 0
) -> Movement:
    return Movement(
        claim=claim,
        movement=movement,
        amount=Decimal(amount),
        currency=currency,
        raw_amount=amount,
        source_indices=(index,),
    )


# --------------------------------------------------------------------------
# Parser layer
# --------------------------------------------------------------------------


def parser_rounding(harness: Harness) -> None:
    label = "parser:rounding"
    cases = {
        "0.004": 0,
        "0.005": 1,
        "0.014": 1,
        "0.015": 2,
        "-0.004": 0,
        "-0.005": -1,
        "-0.015": -2,
        "2.675": 268,
        "1.005": 101,
        "0": 0,
        "-0.00": 0,
        "10": 1000,
        ".5": 50,
        "5.": 500,
    }
    for text, expected in cases.items():
        harness.equal(f"{label}:{text}", expected, prod_parser.amount_to_cents(text))


def parser_significance(harness: Harness) -> None:
    """Very small non-zero values must not silently become zero loss."""
    label = "parser:significance"
    for text in ("0.001", "0.004", "-0.001", "-0.004"):
        harness.equal(f"{label}:{text}", 0, prod_parser.amount_to_cents(text))
    for text in ("0.005", "-0.005"):
        harness.check(f"{label}:{text} nonzero", prod_parser.amount_to_cents(text) != 0, text)


def parser_rejects_invalid(harness: Harness) -> None:
    label = "parser:rejects"
    invalid = ["nan", "NaN", "inf", "Infinity", "", " ", "1e3", "1,000", "--1", "5..0", "0x10", None, True]
    for value in invalid:
        raised = False
        try:
            prod_parser.amount_to_cents(value)
        except prod_parser.FieldError:
            raised = True
        harness.check(f"{label}:amount:{value!r}", raised, "invalid amount was accepted")

    for value in ["US", "USDD", "12", "", "U S", None, 123, True]:
        raised = False
        try:
            prod_parser.normalize_currency(value)
        except prod_parser.FieldError:
            raised = True
        harness.check(f"{label}:currency:{value!r}", raised, "invalid currency was accepted")

    harness.equal(f"{label}:currency normalizes", "USD", prod_parser.normalize_currency(" usd "))


def parser_conflict_detection(harness: Harness) -> None:
    label = "parser:conflict"
    payload = {
        "losses": [
            {"claim": "C", "movement": "M", "amount": "10.00", "currency": "USD"},
            {"claim": "C", "movement": "M", "amount": "10.004", "currency": "usd"},
            {"claim": "C", "movement": "M", "amount": "10.00", "currency": "USD"},
        ],
        "treaties": [],
    }
    parsed = prod_parser.parse(payload)
    # 10.004 rounds to 10.00 and USD==usd, so all three are replays of one key.
    harness.equal(f"{label}:movements", 1, len(parsed.movements))
    harness.equal(f"{label}:replays", 2, parsed.replay_count)
    harness.equal(f"{label}:conflicts", 0, parsed.conflict_count)
    harness.equal(f"{label}:source indices", (0, 1, 2), parsed.movements[0].source_indices)

    conflicting = copy.deepcopy(payload)
    conflicting["losses"][1]["amount"] = "10.01"
    parsed = prod_parser.parse(conflicting)
    harness.equal(f"{label}:conflict movements", 0, len(parsed.movements))
    harness.equal(f"{label}:conflict count", 1, parsed.conflict_count)
    harness.check(
        f"{label}:conflict error",
        any(error["code"] == "movement_conflict" for error in parsed.errors),
        repr(parsed.errors),
    )


def parser_ledger_is_complete(harness: Harness) -> None:
    label = "parser:ledger"
    payload = {
        "losses": [
            {"claim": "C", "movement": "OK", "amount": "1", "currency": "USD"},
            {"claim": "C", "movement": "BAD", "amount": "x", "currency": "USD"},
            {"claim": "C", "movement": "MISSING", "currency": "USD"},
            {"claim": "C", "movement": "DUP", "amount": "2", "currency": "USD"},
            {"claim": "C", "movement": "DUP", "amount": "3", "currency": "USD"},
        ],
        "treaties": [],
    }
    parsed = prod_parser.parse(payload)
    statuses = sorted(entry["status"] for entry in parsed.ledger)
    # Ledger entries are per movement key, so a conflict covers two source rows.
    covered = sorted(index for entry in parsed.ledger for index in entry["source_indices"])
    harness.equal(
        f"{label}:every source row accounted for",
        list(range(len(payload["losses"]))),
        covered,
    )
    harness.equal(f"{label}:statuses", ["applied", "conflict", "rejected", "rejected"], statuses)
    harness.check(
        f"{label}:ledger is ordered by source",
        [entry["source_indices"][0] for entry in parsed.ledger]
        == sorted(entry["source_indices"][0] for entry in parsed.ledger),
        repr(parsed.ledger),
    )


# --------------------------------------------------------------------------
# Engine layer
# --------------------------------------------------------------------------


def engine_replay_is_idempotent(harness: Harness) -> None:
    label = "engine:replay"
    treaty = build_treaty("L1", "0", "1000")
    movement = build_movement("C", "M", "100.00", index=0)
    replay = build_movement("C", "M", "100.00", index=1)

    once = prod_engine.calculate([movement], [treaty])
    twice = prod_engine.calculate([movement, replay], [treaty])
    harness.equal(f"{label}:applied once", once["claims"], twice["claims"])
    harness.equal(f"{label}:allocations unchanged", once["allocations"], twice["allocations"])
    harness.equal(f"{label}:replay recorded", 1, len(twice["replay_skips"]))
    harness.equal(f"{label}:dedupe disabled double-counts", 20000, prod_engine.calculate([movement, replay], [treaty], dedupe=False)["claims"][0]["cumulative_cents"])


def engine_conflicting_replay_rejected(harness: Harness) -> None:
    label = "engine:conflict"
    treaty = build_treaty("L1", "0", "1000")
    raised = False
    try:
        prod_engine.calculate(
            [build_movement("C", "M", "100.00"), build_movement("C", "M", "101.00", index=1)],
            [treaty],
        )
    except prod_engine.DuplicateConflictError:
        raised = True
    harness.check(f"{label}:raises", raised, "conflicting duplicate silently accepted")


def engine_is_order_independent(harness: Harness) -> None:
    label = "engine:order"
    treaties = [build_treaty("L1", "100", "50", index=0), build_treaty("L2", "150", "100", index=1)]
    movements: List[Movement] = [
        build_movement("C1", "M1", "120.00", index=0),
        build_movement("C1", "M2", "80.00", index=1),
        build_movement("C1", "M3", "-30.00", index=2),
        build_movement("C2", "M1", "60.00", index=3),
        build_movement("C2", "M2", "40.00", index=4),
    ]
    baseline = prod_engine.calculate(movements, treaties)
    rng = random.Random(7)
    for attempt in range(20):
        shuffled = movements[:]
        rng.shuffle(shuffled)
        result = prod_engine.calculate(shuffled, treaties)
        harness.equal(f"{label}:shuffle {attempt}", baseline["allocations"], result["allocations"])
        harness.equal(f"{label}:claims {attempt}", baseline["claims"], result["claims"])


def engine_negative_is_signed(harness: Harness) -> None:
    label = "engine:negative"
    treaty = build_treaty("L1", "0", "1000")
    result = prod_engine.calculate(
        [build_movement("C", "M1", "100.00", index=0), build_movement("C", "M2", "-40.00", index=1)],
        [treaty],
    )
    harness.equal(f"{label}:cumulative", 6000, result["claims"][0]["cumulative_cents"])
    harness.equal(
        f"{label}:negative allocation",
        -4000,
        [row for row in result["allocations"] if row["movement"] == "M2"][0]["allocated_cents"],
    )


def engine_currency_scoping(harness: Harness) -> None:
    label = "engine:currency"
    treaties = [build_treaty("USD-ONLY", "0", "100", currency="USD", index=0)]
    result = prod_engine.calculate(
        [
            build_movement("C", "M1", "100.00", currency="USD", index=0),
            build_movement("C", "M2", "100.00", currency="EUR", index=1),
        ],
        treaties,
    )
    rows = {(row["movement"], row["currency"]) for row in result["allocations"]}
    harness.equal(f"{label}:only USD allocated", {("M1", "USD")}, rows)
    harness.equal(
        f"{label}:EUR unallocated",
        10000,
        [claim for claim in result["claims"] if claim["currency"] == "EUR"][0]["unallocated_cents"],
    )


def engine_attachment_semantics(harness: Harness) -> None:
    label = "engine:attachment"
    treaty = build_treaty("L1", "100", "50")
    for text, expected in (
        ("99.99", 0),
        ("100.00", 0),
        ("100.01", 1),
        ("149.99", 4999),
        ("150.00", 5000),
        ("150.01", 5000),
        ("1000.00", 5000),
    ):
        result = prod_engine.calculate([build_movement("C", "M", text)], [treaty])
        total = sum(row["allocated_cents"] for row in result["allocations"])
        harness.equal(f"{label}:{text}", expected, total)


def engine_conservation(harness: Harness) -> None:
    """For every scenario the engine's own totals must reconcile."""
    label = "engine:conservation"
    rng = random.Random(99)
    for attempt in range(50):
        treaties = [
            build_treaty("L1", str(rng.choice([0, 50, 100])), str(rng.choice([25, 50, 100])), index=0)
        ]
        if rng.random() < 0.5:
            attachment = int(treaties[0].attachment.scaleb(2)) + int(treaties[0].limit.scaleb(2))
            treaties.append(build_treaty("L2", str(Decimal(attachment).scaleb(-2)), "100", index=1))
        movements = [
            build_movement(
                rng.choice(["C1", "C2"]),
                f"M{index}",
                f"{rng.randint(-400, 400)}.{rng.randint(0, 99):02d}",
                currency=rng.choice(["USD", "EUR"]),
                index=index,
            )
            for index in range(rng.randint(0, 6))
        ]
        result = prod_engine.calculate(movements, treaties)
        net = sum(claim["cumulative_cents"] for claim in result["claims"])
        allocated = sum(claim["allocated_cents"] for claim in result["claims"])
        unallocated = sum(claim["unallocated_cents"] for claim in result["claims"])
        harness.equal(f"{label}:{attempt}", net, allocated + unallocated)
        harness.equal(
            f"{label}:{attempt}:allocations",
            allocated,
            sum(row["allocated_cents"] for row in result["allocations"]),
        )


# --------------------------------------------------------------------------
# Report layer
# --------------------------------------------------------------------------


def report_status_mapping(harness: Harness) -> None:
    label = "report:status"
    harness.equal(f"{label}:ok", "ok", prod_report.build_result("p", {})["status"])
    harness.equal(
        f"{label}:partial",
        "partial",
        prod_report.build_result("p", {}, errors=[{"code": "x", "message": "", "source": "", "index": None}])["status"],
    )
    harness.equal(
        f"{label}:failed",
        "failed",
        prod_report.build_result(
            "p", {}, fatal_errors=[{"code": "x", "message": "", "source": "", "index": None}]
        )["status"],
    )


def report_has_stable_shape(harness: Harness) -> None:
    label = "report:shape"
    payload = {
        "losses": [{"claim": "C", "movement": "M", "amount": "10", "currency": "USD"}],
        "treaties": [{"name": "L1", "attachment": 0, "limit": 100}],
    }
    result = run_pipeline(payload)
    required = {
        "project",
        "status",
        "errors",
        "warnings",
        "allocations",
        "claims",
        "currency_totals",
        "treaty_totals",
        "replay_skips",
        "counts",
        "audit",
    }
    harness.check(f"{label}:keys", required <= set(result), f"missing {required - set(result)}")
    harness.check(f"{label}:project", result["project"] == "reinsurance-bordereaux-normalizer", result["project"])
    # The report must be JSON-serialisable and deterministic.
    import json

    first = json.dumps(result, sort_keys=True)
    second = json.dumps(run_pipeline(payload), sort_keys=True)
    harness.equal(f"{label}:deterministic", first, second)


def report_audit_trace_provenance(harness: Harness) -> None:
    label = "report:trace"
    payload = {
        "losses": [
            {"claim": "C1", "movement": "M1", "amount": "120", "currency": "USD"},
            {"claim": "C1", "movement": "M2", "amount": "80", "currency": "USD"},
        ],
        "treaties": [{"name": "L1", "attachment": 100, "limit": 50}],
    }
    result = run_pipeline(payload)
    trace = result["audit"]["trace"]
    allocations = result["allocations"]
    harness.equal(f"{label}:one trace per allocation", len(allocations), len(trace))
    for allocation, entry in zip(
        sorted(allocations, key=lambda row: (row["claim"], row["movement"], row["treaty"])),
        sorted(trace, key=lambda row: (row["claim"], row["movement"], row["treaty"])),
    ):
        for field in ("claim", "movement", "currency", "treaty", "allocated_cents", "claim_cumulative_cents"):
            harness.equal(f"{label}:{field}:{allocation['movement']}", allocation[field], entry[field])
        harness.equal(
            f"{label}:sources:{allocation['movement']}",
            allocation["sources"],
            entry["source_indices"],
        )


def report_fatal_reports_code(harness: Harness) -> None:
    label = "report:fatal-code"
    payload = {
        "losses": [],
        "treaties": [
            {"name": "L1", "attachment": 0, "limit": 100},
            {"name": "L2", "attachment": 50, "limit": 100},
        ],
    }
    result = run_pipeline(payload)
    harness.equal(f"{label}:failed", "failed", result["status"])
    codes = {error["code"] for error in result["fatal_errors"]}
    harness.check(f"{label}:overlap code", "TREATY_OVERLAP" in codes, repr(codes))
    harness.equal(f"{label}:nothing allocated", [], result["allocations"])


def report_partial_reports_codes(harness: Harness) -> None:
    label = "report:partial-codes"
    payload = {
        "losses": [
            {"claim": "C", "movement": "M1", "amount": "10", "currency": "US"},
            {"claim": "C", "movement": "M2", "amount": "10", "currency": "USD"},
        ],
        "treaties": [],
    }
    result = run_pipeline(payload)
    harness.equal(f"{label}:partial", "partial", result["status"])
    codes = {error["code"] for error in result["errors"]}
    harness.check(f"{label}:currency code", "INVALID_CURRENCY" in codes, repr(codes))
    harness.equal(f"{label}:valid row applied", 1, result["counts"]["applied_movements"])


def report_conservation_failure_fails_closed(harness: Harness) -> None:
    """If reconciliation ever fails, the run must not publish allocations."""
    label = "report:conservation-failure"
    payload = {
        "losses": [{"claim": "C", "movement": "M", "amount": "100", "currency": "USD"}],
        "treaties": [{"name": "L1", "attachment": 0, "limit": 100}],
    }
    original = prod_report.build_audit

    def sabotage(parse_result, engine_result, treaties, engine_applied=None):
        audit = original(parse_result, engine_result, treaties, engine_applied=engine_applied)
        audit["conservation"]["all_checks_pass"] = False
        return audit

    prod_report.build_audit = sabotage
    try:
        result = prod_pipeline.run(copy.deepcopy(payload))
    finally:
        prod_report.build_audit = original

    harness.equal(f"{label}: status", "failed", result["status"])
    harness.equal(f"{label}: nothing published", [], result["allocations"])
    harness.check(
        f"{label}: failure code",
        any(error["code"] == "CONSERVATION_FAILURE" for error in result.get("fatal_errors", [])),
        repr(result.get("fatal_errors")),
    )


LAYER_CHECKS = [
    parser_rounding,
    parser_significance,
    parser_rejects_invalid,
    parser_conflict_detection,
    parser_ledger_is_complete,
    engine_replay_is_idempotent,
    engine_conflicting_replay_rejected,
    engine_is_order_independent,
    engine_negative_is_signed,
    engine_currency_scoping,
    engine_attachment_semantics,
    engine_conservation,
    report_status_mapping,
    report_has_stable_shape,
    report_audit_trace_provenance,
    report_fatal_reports_code,
    report_partial_reports_codes,
    report_conservation_failure_fails_closed,
]
