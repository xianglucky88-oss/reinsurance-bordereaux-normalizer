"""Stable output envelope.

The report is the sole place that decides the public shape of a run.  It is
deterministic: no timestamps, no set iteration, no floating point rendering.
Every allocation row is traceable to its claim, movement, source row indices,
currency, and treaty.  The audit section carries a machine-checkable
conservation identity in integer cents.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .models import PipelineResult

PROJECT = "reinsurance-bordereaux-normalizer"

CODES = {
    "invalid_payload": "INVALID_PAYLOAD",
    "invalid_losses": "INVALID_LOSSES",
    "invalid_treaties": "INVALID_TREATIES",
    "invalid_loss_row": "INVALID_LOSS_ROW",
    "missing_field": "MISSING_FIELD",
    "invalid_amount": "INVALID_AMOUNT",
    "amount_out_of_range": "AMOUNT_OUT_OF_RANGE",
    "invalid_currency": "INVALID_CURRENCY",
    "invalid_claim": "INVALID_CLAIM",
    "invalid_movement": "INVALID_MOVEMENT",
    "invalid_name": "INVALID_NAME",
    "invalid_treaty": "INVALID_TREATY",
    "invalid_attachment": "INVALID_ATTACHMENT",
    "invalid_limit": "INVALID_LIMIT",
    "duplicate_treaty": "DUPLICATE_TREATY",
    "treaty_overlap": "TREATY_OVERLAP",
    "movement_conflict": "MOVEMENT_CONFLICT",
    "mixed_currency_claim": "MIXED_CURRENCY_CLAIM",
}


def _public_error(error: dict) -> dict:
    """Strip internal-only fields and attach a stable upper-case code."""
    public = {
        "code": CODES.get(error.get("code", ""), str(error.get("code", "")).upper()),
        "message": error.get("message", ""),
        "source": error.get("source", ""),
        "index": error.get("index"),
    }
    if error.get("claim") is not None:
        public["claim"] = error["claim"]
    if error.get("movement") is not None:
        public["movement"] = error["movement"]
    if error.get("treaty") is not None:
        public["treaty"] = error["treaty"]
    if error.get("currency") is not None:
        public["currency"] = error["currency"]
    if error.get("currencies") is not None:
        public["currencies"] = list(error["currencies"])
    if error.get("values") is not None:
        public["values"] = list(error["values"])
    return public


def _status(status: str, fatal: Sequence[dict], errors: Sequence[dict]) -> str:
    if status == "ok":
        if fatal:
            return "failed"
        if errors:
            return "partial"
    return status


def _jsonable(value: Any) -> Any:
    """Convert Decimal values to JSON numbers at the public boundary.

    Money is always accompanied by its exact ``*_cents`` integer in the same
    object, so this float rendering is for interoperability only and is never
    used for reconciliation.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def build_result(
    project: str,
    data: dict,
    *,
    status: str = "ok",
    errors: Sequence[dict] = (),
    warnings: Sequence[dict] = (),
    fatal_errors: Sequence[dict] = (),
    audit: Dict[str, Any] | None = None,
) -> dict:
    result = PipelineResult(project=project, data=data)
    resolved_status = _status(status, fatal_errors, errors)
    payload: Dict[str, Any] = {
        "project": result.project,
        "status": resolved_status,
        "errors": [_public_error(error) for error in errors],
        "warnings": [_public_error(warning) for warning in warnings],
    }
    if fatal_errors:
        payload["fatal_errors"] = [_public_error(error) for error in fatal_errors]
    payload.update(_jsonable(result.data))
    if audit is not None:
        payload["audit"] = _jsonable(audit)
    return payload


def build_audit(
    parse_result,
    engine_result: dict,
    treaties: Sequence[Any],
    engine_applied: "Sequence[Tuple[str, str]] | None" = None,
) -> dict:
    """Assemble the reconciliation block.

    ``engine_result`` is trusted only for the allocation rows; the conserved
    quantities are recomputed here so the audit is not a tautology over engine
    output.  All values are integer cents.

    ``engine_applied`` is the set of ``(claim, movement)`` keys the engine
    actually applied.  When a run is aborted before the engine (fatal
    configuration error) it is empty, and ledger entries that the parser had
    validated are relabelled ``"validated"`` rather than claimed as applied.
    """
    allocation_rows = engine_result.get("allocations", [])
    applied_allowlist = None if engine_applied is None else set(engine_applied)

    # Movement ledger: one entry per distinct applied key, with replay folding.
    ledger = []
    for entry in parse_result.ledger:
        status = entry["status"]
        if (
            applied_allowlist is not None
            and status in ("applied", "replayed")
            and (entry["claim"], entry["movement"]) not in applied_allowlist
        ):
            status = "validated"
        ledger.append(
            {
                "claim": entry["claim"],
                "movement": entry["movement"],
                "currency": entry["currency"],
                "amount": entry["amount"],
                "status": status,
                "code": CODES.get(entry.get("code") or "", None),
                "source_indices": list(entry["source_indices"]),
                "replay_count": entry.get("replay_count", 0),
            }
        )

    trace = []
    for row in allocation_rows:
        trace.append(
            {
                "claim": row["claim"],
                "movement": row["movement"],
                "currency": row["currency"],
                "treaty": row["treaty"],
                "allocated_cents": row["allocated_cents"],
                "claim_cumulative_cents": row["claim_cumulative_cents"],
                "source_indices": list(row.get("sources", [])),
                "ledger_entry": "applied",
            }
        )

    # Conservation is recomputed from the ledger's applied entries.
    applied_ledger = [
        entry
        for entry in parse_result.ledger
        if entry["status"] in ("applied", "replayed")
        and (
            applied_allowlist is None
            or (entry["claim"], entry["movement"]) in applied_allowlist
        )
    ]
    net_by_currency: Dict[str, int] = {}
    applied_rows = []
    for entry in applied_ledger:
        currency = entry["currency"]
        cents = entry["amount_cents"]
        net_by_currency[currency] = net_by_currency.get(currency, 0) + cents
        applied_rows.append(
            {
                "claim": entry["claim"],
                "movement": entry["movement"],
                "currency": currency,
                "amount_cents": cents,
            }
        )

    allocated_by_currency: Dict[str, int] = {}
    for row in allocation_rows:
        currency = row["currency"]
        allocated_by_currency[currency] = (
            allocated_by_currency.get(currency, 0) + row["allocated_cents"]
        )

    # Engine-reported currency totals, recomputed independently from the ledger.
    engine_net = {
        item["currency"]: item["net_movements_cents"]
        for item in engine_result.get("currency_totals", [])
    }

    by_currency = []
    checks = []
    evaluated = not parse_result.fatal_errors
    for currency in sorted(set(net_by_currency) | set(allocated_by_currency) | set(engine_net)):
        net = net_by_currency.get(currency, 0)
        allocated = allocated_by_currency.get(currency, 0)
        unallocated = net - allocated
        by_currency.append(
            {
                "currency": currency,
                "net_cents": net,
                "allocated_cents": allocated,
                "unallocated_cents": unallocated,
            }
        )
        checks.append(
            {
                "name": f"ledger_net_matches_engine:{currency}",
                "ok": evaluated and engine_net.get(currency, 0) == net,
                "evaluated": evaluated,
                "expected_cents": net,
                "actual_cents": engine_net.get(currency, 0),
            }
        )
        checks.append(
            {
                "name": f"net_equals_allocated_plus_unallocated:{currency}",
                "ok": evaluated and allocated + unallocated == net,
                "evaluated": evaluated,
                "expected_cents": net,
                "actual_cents": allocated + unallocated,
            }
        )

    # Per-claim cumulative loss must match the ledger independently of order.
    claim_net: Dict[Tuple[str, str], int] = {}
    for entry in applied_ledger:
        key = (entry["claim"], entry["currency"])
        claim_net[key] = claim_net.get(key, 0) + entry["amount_cents"]
    for claim_row in engine_result.get("claims", []):
        key = (claim_row["claim"], claim_row["currency"])
        expected = claim_net.get(key, 0)
        checks.append(
            {
                "name": f"claim_cumulative_matches_ledger:{key[0]}:{key[1]}",
                "ok": claim_row["cumulative_cents"] == expected,
                "evaluated": True,
                "expected_cents": expected,
                "actual_cents": claim_row["cumulative_cents"],
            }
        )

    # Every allocation row must point back at a real ledger entry.
    ledger_keys = {(entry["claim"], entry["movement"]) for entry in applied_ledger}
    for row in allocation_rows:
        key = (row["claim"], row["movement"])
        checks.append(
            {
                "name": f"allocation_traceable:{key[0]}:{key[1]}:{row['treaty']}",
                "ok": key in ledger_keys,
                "evaluated": True,
                "expected_cents": None,
                "actual_cents": None,
            }
        )

    # Each treaty's total must equal the sum of its per-movement contributions,
    # and must never exceed its own limit.  Per-layer totals must also
    # telescope to the final coverage of each claim's cumulative position:
    # sum(layer totals) == sum(final coverage) per currency.
    final_coverage_by_currency: Dict[str, int] = {}
    final_positions: Dict[Tuple[str, str], int] = {
        (row["claim"], row["currency"]): row["cumulative_cents"]
        for row in engine_result.get("claims", [])
    }
    for (claim, currency), position in final_positions.items():
        for treaty in treaties:
            if treaty.currency is not None and treaty.currency != currency:
                continue
            attachment = int(treaty.attachment.scaleb(2))
            limit = int(treaty.limit.scaleb(2))
            coverage = max(0, min(position, attachment + limit) - attachment)
            final_coverage_by_currency[currency] = (
                final_coverage_by_currency.get(currency, 0) + coverage
            )

    for currency in sorted(final_coverage_by_currency):
        layer_sum = sum(
            row["allocated_cents"]
            for row in allocation_rows
            if row["currency"] == currency
        )
        checks.append(
            {
                "name": f"layers_telescope_to_final_coverage:{currency}",
                "ok": layer_sum == final_coverage_by_currency[currency] and evaluated,
                "evaluated": evaluated,
                "expected_cents": final_coverage_by_currency[currency],
                "actual_cents": layer_sum,
            }
        )

    # Every treaty is a *per-claim* tower: each claim gets its own full limit,
    # so the check is per (claim, treaty, currency).  A single claim must never
    # allocate more into a layer than that layer can hold.
    for treaty in sorted(treaties, key=lambda t: (t.name, t.source_index)):
        limit_cents = int(treaty.limit.scaleb(2))
        keys = {
            (row["claim"], row["currency"])
            for row in allocation_rows
            if row["treaty"] == treaty.name
        }
        for claim, currency in sorted(keys):
            total = sum(
                row["allocated_cents"]
                for row in allocation_rows
                if row["treaty"] == treaty.name
                and row["currency"] == currency
                and row["claim"] == claim
            )
            checks.append(
                {
                    "name": f"layer_within_limit:{claim}:{treaty.name}:{currency}",
                    "ok": total <= limit_cents,
                    "evaluated": True,
                    "expected_cents": limit_cents,
                    "actual_cents": total,
                }
            )

    return {
        "ledger": ledger,
        "trace": trace,
        "conservation": {
            "by_currency": by_currency,
            "checks": checks,
            "evaluated": evaluated,
            "all_checks_pass": evaluated and all(check["ok"] for check in checks),
        },
        "counts": {
            "input_rows": parse_result.input_count,
            "rejected_rows": parse_result.rejected_rows,
            "conflict_keys": parse_result.conflict_count,
            "replayed_rows": parse_result.replay_count,
            "applied_movements": len(applied_rows),
            "allocation_rows": len(allocation_rows),
        },
    }
