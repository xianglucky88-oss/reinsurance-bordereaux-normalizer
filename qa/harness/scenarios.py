"""Scenario suite: named, auditable cases for every settlement semantic.

Each scenario is a small payload plus explicit expectations.  Expectations are
written against the *specification*, not against whatever the production code
currently returns, so a regression fails the check rather than being blessed.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from .core import (
    Harness,
    allocations_by_key,
    assert_idempotent_replay,
    assert_invariants,
    assert_matches_reference,
    assert_permutation_invariance,
    run_pipeline,
)

# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------


def loss(claim: str, movement: str, amount: Any, currency: str = "USD", **extra: Any) -> dict:
    row = {"claim": claim, "movement": movement, "amount": amount, "currency": currency}
    row.update(extra)
    return row


def treaty(name: str, attachment: Any, limit: Any, currency: str | None = None) -> dict:
    row: Dict[str, Any] = {"name": name, "attachment": attachment, "limit": limit}
    if currency is not None:
        row["currency"] = currency
    return row


def payload(losses: List[dict], treaties: List[dict]) -> dict:
    return {"losses": losses, "treaties": treaties}


# --------------------------------------------------------------------------
# Individual scenarios
# --------------------------------------------------------------------------


def scenario_movement_increase_and_decrease(harness: Harness) -> None:
    """Increases and decreases must both move the claim's cumulative loss."""
    case = payload(
        [
            loss("C1", "M1", "100.00"),
            loss("C1", "M2", "40.00"),
            loss("C1", "M3", "-25.00"),
        ],
        [treaty("L1", "0", "1000")],
    )
    result = run_pipeline(case)
    label = "movement-increase-and-decrease"

    harness.equal(f"{label}: cumulative", 11500, result["claims"][0]["cumulative_cents"])
    harness.equal(
        f"{label}: movement allocations",
        {("C1", "M1", "USD", "L1"): 10000, ("C1", "M2", "USD", "L1"): 4000, ("C1", "M3", "USD", "L1"): -2500},
        allocations_by_key(result),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_negative_is_not_new_loss(harness: Harness) -> None:
    """A decrease below the layer must reduce coverage, not add new loss."""
    case = payload(
        [loss("C1", "M1", "150.00"), loss("C1", "M2", "-100.00")],
        [treaty("L1", "100", "100")],
    )
    result = run_pipeline(case)
    label = "negative-is-not-new-loss"

    # 150 -> 50 crosses the 100 attachment downwards.
    harness.equal(
        f"{label}: allocations",
        {("C1", "M1", "USD", "L1"): 5000, ("C1", "M2", "USD", "L1"): -5000},
        allocations_by_key(result),
    )
    harness.equal(f"{label}: cumulative", 5000, result["claims"][0]["cumulative_cents"])
    harness.equal(f"{label}: unallocated", 5000, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_partial_layer(harness: Harness) -> None:
    """A loss that straddles two layers must be split exactly at the boundary."""
    case = payload(
        [loss("C1", "M1", "180.00")],
        [treaty("L1", "100", "50"), treaty("L2", "150", "100")],
    )
    result = run_pipeline(case)
    label = "partial-layer"
    harness.equal(
        f"{label}: allocations",
        {("C1", "M1", "USD", "L1"): 5000, ("C1", "M1", "USD", "L2"): 3000},
        allocations_by_key(result),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_attachment_boundary(harness: Harness) -> None:
    """At the attachment point coverage is zero; one cent above it is covered."""
    base = [treaty("L1", "100", "50")]
    for cents, expected in (("100.00", 0), ("100.01", 1), ("150.00", 5000), ("150.01", 5000)):
        case = payload([loss("C1", "M1", cents)], base)
        result = run_pipeline(case)
        label = f"attachment-boundary:{cents}"
        harness.equal(f"{label}: total allocated", expected, sum(allocations_by_key(result).values()))
        assert_matches_reference(harness, case, result, label)
        assert_invariants(harness, case, result, label)


def scenario_above_top_layer(harness: Harness) -> None:
    """Loss above the tower is unallocated, never allocated beyond the limit."""
    case = payload(
        [loss("C1", "M1", "1000.00")],
        [treaty("L1", "0", "100"), treaty("L2", "100", "100")],
    )
    result = run_pipeline(case)
    label = "above-top-layer"
    harness.equal(f"{label}: allocated", 20000, sum(allocations_by_key(result).values()))
    harness.equal(f"{label}: unallocated", 80000, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_tower_gap(harness: Harness) -> None:
    """A gap between layers must be unallocated, not quietly filled."""
    case = payload(
        [loss("C1", "M1", "250.00")],
        [treaty("L1", "0", "100"), treaty("L2", "150", "100")],
    )
    result = run_pipeline(case)
    label = "tower-gap"
    harness.equal(f"{label}: allocated", 20000, sum(allocations_by_key(result).values()))
    harness.equal(f"{label}: cumulative", 25000, result["claims"][0]["cumulative_cents"])
    harness.equal(f"{label}: unallocated", 5000, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_replay(harness: Harness) -> None:
    """The same movement reported twice must count once and be audited."""
    single = payload([loss("C1", "M1", "100.00")], [treaty("L1", "0", "1000")])
    repeated = payload(
        [loss("C1", "M1", "100.00"), loss("C1", "M1", "100.00")],
        [treaty("L1", "0", "1000")],
    )
    result = run_pipeline(repeated)
    label = "replay"
    harness.equal(f"{label}: status", "ok", result["status"])
    harness.equal(f"{label}: allocations", allocations_by_key(run_pipeline(single)), allocations_by_key(result))
    harness.equal(f"{label}: cumulative", 10000, result["claims"][0]["cumulative_cents"])
    harness.equal(f"{label}: replayed rows", 1, result["audit"]["counts"]["replayed_rows"])
    assert_matches_reference(harness, repeated, result, label)
    assert_invariants(harness, repeated, result, label)
    assert_idempotent_replay(harness, single, label)


def scenario_conflicting_replay(harness: Harness) -> None:
    """A key replayed with a different value must be excluded, not double-counted."""
    case = payload(
        [
            loss("C1", "M1", "100.00"),
            loss("C1", "M1", "120.00"),
            loss("C1", "M2", "10.00"),
        ],
        [treaty("L1", "0", "1000")],
    )
    result = run_pipeline(case)
    label = "conflicting-replay"
    harness.equal(f"{label}: status", "partial", result["status"])
    harness.equal(f"{label}: only clean movement applied", {("C1", "M2", "USD", "L1"): 1000}, allocations_by_key(result))
    harness.equal(f"{label}: conflict keys", 1, result["audit"]["counts"]["conflict_keys"])
    harness.check(
        f"{label}: conflict is reported",
        any(error["code"] == "MOVEMENT_CONFLICT" for error in result["errors"]),
        repr(result["errors"]),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_currency_separation(harness: Harness) -> None:
    """No FX: currencies are normalized independently and never summed."""
    case = payload(
        [
            loss("C1", "M1", "100.00", "USD"),
            loss("C1", "M2", "50.00", "EUR"),
            loss("C1", "M3", "25.00", "usd"),
        ],
        [treaty("L1", "0", "1000")],
    )
    result = run_pipeline(case)
    label = "currency-separation"
    totals = {row["currency"]: row["net_movements_cents"] for row in result["currency_totals"]}
    harness.equal(f"{label}: currency totals", {"EUR": 5000, "USD": 12500}, totals)
    harness.check(
        f"{label}: mixed currency warned",
        any(warning["code"] == "MIXED_CURRENCY_CLAIM" for warning in result["warnings"]),
        repr(result["warnings"]),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_currency_scoped_layer(harness: Harness) -> None:
    """A currency-scoped layer must not absorb other currencies' movements."""
    case = payload(
        [loss("C1", "M1", "100.00", "USD"), loss("C1", "M2", "100.00", "EUR")],
        [treaty("L1", "0", "100", "USD")],
    )
    result = run_pipeline(case)
    label = "currency-scoped-layer"
    harness.equal(
        f"{label}: only USD layer",
        {("C1", "M1", "USD", "L1"): 10000},
        allocations_by_key(result),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_empty_treaty(harness: Harness) -> None:
    """No treaties means everything is unallocated, not an error."""
    case = payload([loss("C1", "M1", "100.00")], [])
    result = run_pipeline(case)
    label = "empty-tower"
    harness.equal(f"{label}: status", "ok", result["status"])
    harness.equal(f"{label}: no allocations", {}, allocations_by_key(result))
    harness.equal(f"{label}: unallocated", 10000, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_rounding_boundary(harness: Harness) -> None:
    """Half-up rounding at the cent boundary, including negative halves."""
    cases = {
        "0.005": 1,
        "0.004": 0,
        "-0.005": -1,
        "-0.004": 0,
        "1.005": 101,
        "2.675": 268,
        "0.015": 2,
    }
    for text, expected in cases.items():
        case = payload([loss("C1", "M1", text)], [treaty("L1", "0", "1000")])
        result = run_pipeline(case)
        label = f"rounding:{text}"
        harness.equal(
            f"{label}: normalized cents",
            expected,
            result["claims"][0]["cumulative_cents"],
        )
        assert_matches_reference(harness, case, result, label)
        assert_invariants(harness, case, result, label)


def scenario_negative_zero(harness: Harness) -> None:
    """``-0.00`` must normalize to a plain zero."""
    case = payload([loss("C1", "M1", "-0.00")], [treaty("L1", "0", "1000")])
    result = run_pipeline(case)
    label = "negative-zero"
    harness.equal(f"{label}: ledger amount", "0.00", result["audit"]["ledger"][0]["amount"])
    harness.equal(f"{label}: cumulative", 0, result["claims"][0]["cumulative_cents"])
    harness.check(
        f"{label}: no negative zero rendered",
        "-0.00" not in str(result),
        "negative zero leaked into the report",
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_illegal_amounts(harness: Harness) -> None:
    """Invalid amounts are rejected at row level and never become zero."""
    bad_values = [
        "not-a-number",
        "",
        "1,000.00",
        "1e3",
        "NaN",
        "Infinity",
        "-Infinity",
        None,
        True,
        ["100"],
        {"amount": 100},
        "  ",
        "--5",
        "5..0",
    ]
    for index, bad in enumerate(bad_values):
        case = payload([loss("C1", f"M{index}", bad)], [treaty("L1", "0", "1000")])
        result = run_pipeline(case)
        label = f"illegal-amount:{bad!r}"
        harness.equal(f"{label}: status", "partial", result.get("status"))
        harness.equal(f"{label}: nothing applied", {}, allocations_by_key(result))
        harness.equal(f"{label}: rejected rows", 1, result["audit"]["counts"]["rejected_rows"])
        assert_matches_reference(harness, case, result, label)
        assert_invariants(harness, case, result, label)


def scenario_out_of_range(harness: Harness) -> None:
    case = payload([loss("C1", "M1", "1000000000001.00")], [treaty("L1", "0", "1")])
    result = run_pipeline(case)
    label = "out-of-range"
    harness.equal(f"{label}: status", "partial", result["status"])
    harness.check(
        f"{label}: range error reported",
        any(error["code"] == "AMOUNT_OUT_OF_RANGE" for error in result["errors"]),
        repr(result["errors"]),
    )
    assert_matches_reference(harness, case, result, label)


def scenario_partial_failure(harness: Harness) -> None:
    """Valid rows must still be processed when other rows fail."""
    case = payload(
        [
            loss("C1", "M1", "100.00"),
            loss("C1", "M2", "garbage"),
            loss("C2", "M1", "50.00"),
            {"claim": "C2", "movement": "M2", "currency": "USD"},
        ],
        [treaty("L1", "0", "1000")],
    )
    result = run_pipeline(case)
    label = "partial-failure"
    harness.equal(f"{label}: status", "partial", result["status"])
    harness.equal(f"{label}: applied movements", 2, result["audit"]["counts"]["applied_movements"])
    harness.equal(f"{label}: rejected rows", 2, result["audit"]["counts"]["rejected_rows"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_permutation(harness: Harness) -> None:
    """Reordering input rows must not change any result."""
    case = payload(
        [
            loss("C1", "M1", "120.00"),
            loss("C1", "M2", "80.00"),
            loss("C2", "M1", "60.00"),
            loss("C1", "M3", "-30.00"),
            loss("C2", "M2", "40.00"),
        ],
        [treaty("L1", "100", "50"), treaty("L2", "150", "100")],
    )
    permutations = [
        [0, 1, 2, 3, 4],
        [4, 3, 2, 1, 0],
        [2, 0, 4, 1, 3],
        [1, 3, 0, 4, 2],
    ]
    assert_permutation_invariance(harness, case, permutations, "permutation")
    result = run_pipeline(case)
    assert_matches_reference(harness, case, result, "permutation")
    assert_invariants(harness, case, result, "permutation")


def scenario_claim_traceability(harness: Harness) -> None:
    """Every allocation must carry claim, movement and treaty identity."""
    case = payload(
        [
            loss("ALPHA", "INC", "120.00"),
            loss("BETA", "REV", "-20.00"),
        ],
        [treaty("L1", "50", "100")],
    )
    result = run_pipeline(case)
    label = "traceability"
    harness.check(f"{label}: allocations exist", bool(result["allocations"]))
    for row in result["allocations"]:
        harness.check(f"{label}: claim id", row["claim"] in {"ALPHA", "BETA"}, repr(row))
        harness.check(f"{label}: movement id", bool(row["movement"]), repr(row))
        harness.check(f"{label}: treaty id", row["treaty"] == "L1", repr(row))
        harness.check(f"{label}: sources present", bool(row["sources"]), repr(row))
    ledger_keys = {(entry["claim"], entry["movement"]) for entry in result["audit"]["ledger"]}
    for row in result["allocations"]:
        harness.check(
            f"{label}: ledger link",
            (row["claim"], row["movement"]) in ledger_keys,
            repr(row),
        )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_reversal_across_layers(harness: Harness) -> None:
    """A reversal that spans two layers must claw back each layer correctly.

    Position 200 sits in L2 (150-250): L1 holds 50 and L2 holds 50.  Reversing
    to 80 drops below L1's attachment, so both layers release everything they
    held (-50 each).  The remaining 20 of the -120 movement was unallocated
    loss above L2's coverage, which also decreases by 20.
    """
    case = payload(
        [loss("C1", "M1", "200.00"), loss("C1", "M2", "-120.00")],
        [treaty("L1", "100", "50"), treaty("L2", "150", "100")],
    )
    result = run_pipeline(case)
    label = "reversal-across-layers"
    harness.equal(
        f"{label}: allocations",
        {
            ("C1", "M1", "USD", "L1"): 5000,
            ("C1", "M1", "USD", "L2"): 5000,
            ("C1", "M2", "USD", "L1"): -5000,
            ("C1", "M2", "USD", "L2"): -5000,
        },
        allocations_by_key(result),
    )
    # Net movement 80 is below L1's attachment, so nothing remains allocated.
    harness.equal(f"{label}: cumulative", 8000, result["claims"][0]["cumulative_cents"])
    harness.equal(f"{label}: net allocated", 0, result["claims"][0]["allocated_cents"])
    harness.equal(f"{label}: unallocated", 8000, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_negative_after_zero(harness: Harness) -> None:
    """A reversal on a claim with no prior loss yields negative unallocated loss."""
    case = payload([loss("C1", "M1", "-50.00")], [treaty("L1", "0", "100")])
    result = run_pipeline(case)
    label = "negative-from-zero"
    harness.equal(f"{label}: cumulative", -5000, result["claims"][0]["cumulative_cents"])
    harness.equal(f"{label}: allocated", 0, result["claims"][0]["allocated_cents"])
    harness.equal(f"{label}: unallocated", -5000, result["claims"][0]["unallocated_cents"])
    harness.check(
        f"{label}: no phantom positive allocation",
        all(row["allocated_cents"] <= 0 for row in result["allocations"]),
        repr(result["allocations"]),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_unattached_loss(harness: Harness) -> None:
    """Loss below the attachment point is entirely unallocated."""
    case = payload([loss("C1", "M1", "99.99")], [treaty("L1", "100", "50")])
    result = run_pipeline(case)
    label = "below-attachment"
    harness.equal(f"{label}: allocated", 0, sum(allocations_by_key(result).values()))
    harness.equal(f"{label}: unallocated", 9999, result["claims"][0]["unallocated_cents"])
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


def scenario_each_claim_has_own_tower(harness: Harness) -> None:
    """Each claim gets its own full tower; limits are not shared across claims.

    Three claims each fill a 50.00 layer.  Per claim that is exactly the limit;
    aggregated across claims it is 150.00, which would look like a limit breach
    if the layer were treated as shared.
    """
    case = payload(
        [
            loss("C1", "M1", "75.00"),
            loss("C2", "M1", "75.00"),
            loss("C3", "M1", "75.00"),
        ],
        [treaty("L1", "0", "50")],
    )
    result = run_pipeline(case)
    label = "per-claim-tower"
    harness.equal(
        f"{label}: each claim fills the layer",
        {
            ("C1", "M1", "USD", "L1"): 5000,
            ("C2", "M1", "USD", "L1"): 5000,
            ("C3", "M1", "USD", "L1"): 5000,
        },
        allocations_by_key(result),
    )
    per_claim = {}
    for row in result["allocations"]:
        per_claim[row["claim"]] = per_claim.get(row["claim"], 0) + row["allocated_cents"]
    for claim in ("C1", "C2", "C3"):
        harness.equal(f"{label}: {claim} at its own limit", 5000, per_claim.get(claim))
    harness.check(
        f"{label}: aggregate exceeds a single layer",
        sum(per_claim.values()) > 5000,
        repr(per_claim),
    )
    harness.check(
        f"{label}: conservation still passes",
        result["audit"]["conservation"]["all_checks_pass"],
        repr(result["audit"]["conservation"]["checks"]),
    )
    assert_matches_reference(harness, case, result, label)
    assert_invariants(harness, case, result, label)


SCENARIOS = [
    scenario_movement_increase_and_decrease,
    scenario_negative_is_not_new_loss,
    scenario_partial_layer,
    scenario_attachment_boundary,
    scenario_above_top_layer,
    scenario_tower_gap,
    scenario_replay,
    scenario_conflicting_replay,
    scenario_currency_separation,
    scenario_currency_scoped_layer,
    scenario_empty_treaty,
    scenario_rounding_boundary,
    scenario_negative_zero,
    scenario_illegal_amounts,
    scenario_out_of_range,
    scenario_partial_failure,
    scenario_permutation,
    scenario_claim_traceability,
    scenario_reversal_across_layers,
    scenario_negative_after_zero,
    scenario_unattached_loss,
    scenario_each_claim_has_own_tower,
]
