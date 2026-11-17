"""Domain calculation only.

All arithmetic is integer cents.  The engine has no knowledge of JSON,
parsing rules, or report formatting.

Semantics
---------
Loss movements are severity movements *per claim per currency*.  There is no FX
conversion: a claim reported in two currencies produces two independent
cumulative losses, and a treaty whose ``currency`` is set only applies to
movements in that currency.

Movements are applied in canonical ``(claim, currency, movement)`` order, not in
input order.  Input order is therefore not part of the contract: permuting the
input produces an identical result, including the per-movement attribution of a
layer allocation.

A movement carries a signed amount.  Negative movements are genuine downward
adjustments: they reduce the claim's running cumulative loss and can reverse a
previously allocated layer amount (claw-back).  A negative movement is never
treated as fresh positive loss.

For each applied movement the layer allocation is the change in that treaty's
coverage between the claim's cumulative loss before and after the movement.
Because coverage telescopes, the per-layer allocations sum to the coverage of
the final cumulative position.  The difference between the normalized net loss
and total layer coverage is reported as unallocated (below attachment, in a
tower gap, or above the top layer).  Both quantities are integer cents, so the
conservation identity is exact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import Movement, Treaty

_CENT = Decimal("0.01")


class DuplicateConflictError(ValueError):
    """The same (claim, movement) key was supplied with different values."""


def to_cents(value: Decimal) -> int:
    """Convert an already-normalized Decimal to integer cents."""
    return int(value.scaleb(2))


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def _coverage_cents(position_cents: int, treaty: Treaty) -> int:
    """Exact layer coverage: clamp(position - attachment, 0, limit), in cents."""
    attachment = to_cents(treaty.attachment)
    limit = to_cents(treaty.limit)
    return max(0, min(position_cents, attachment + limit) - attachment)


def _applicable_treaties(treaties: Sequence[Treaty], currency: str) -> List[Treaty]:
    """Treaties in canonical tower order, restricted to those for ``currency``."""
    applicable = [t for t in treaties if t.currency is None or t.currency == currency]
    return sorted(applicable, key=lambda t: (t.attachment, t.limit, t.name, t.source_index))


def _canonical_movements(movements: Iterable[Movement]) -> List[Movement]:
    return sorted(
        movements,
        key=lambda m: (m.claim, m.currency, m.movement, m.source_indices),
    )


def _dedupe(movements: Sequence[Movement]) -> Tuple[List[Movement], List[dict]]:
    """Collapse exact replays; reject conflicting values for the same key.

    Replays are identical ``(amount, currency)`` reports of one
    ``(claim, movement)`` key; they are applied once.  A differing value for the
    same key is ambiguous and must never be silently double-counted or dropped,
    so it raises :class:`DuplicateConflictError`.
    """
    first: Dict[Tuple[str, str], Movement] = {}
    applied: List[Movement] = []
    replays: List[dict] = []
    for movement in movements:
        key = (movement.claim, movement.movement)
        previous = first.get(key)
        if previous is None:
            first[key] = movement
            applied.append(movement)
            continue
        if (previous.amount, previous.currency) != (movement.amount, movement.currency):
            raise DuplicateConflictError(
                f"conflicting duplicate movement {key[0]!r}/{key[1]!r}: "
                f"{previous.amount} {previous.currency} vs {movement.amount} {movement.currency}"
            )
        replays.append(
            {
                "claim": movement.claim,
                "movement": movement.movement,
                "currency": movement.currency,
                "amount": str(movement.amount),
                "discarded_source_indices": list(movement.source_indices),
            }
        )
    return applied, replays


def calculate(
    movements: Iterable[Movement],
    treaties: Sequence[Treaty],
    *,
    dedupe: bool = True,
) -> dict:
    """Normalize movements and allocate them across treaty layers."""
    ordered = _canonical_movements(movements)
    if dedupe:
        ordered, replay_skips = _dedupe(ordered)
    else:
        replay_skips = []

    cumulative: Dict[Tuple[str, str], int] = {}
    claim_allocated: Dict[Tuple[str, str], int] = {}
    currency_totals: Dict[str, int] = {}
    treaty_totals: Dict[Tuple[str, str], dict] = {}
    rows: List[dict] = []

    for movement in ordered:
        claim = movement.claim
        currency = movement.currency
        key = (claim, currency)
        before = cumulative.get(key, 0)
        after = before + to_cents(movement.amount)
        cumulative[key] = after
        currency_totals[currency] = currency_totals.get(currency, 0) + to_cents(movement.amount)

        for treaty in _applicable_treaties(treaties, currency):
            allocated = _coverage_cents(after, treaty) - _coverage_cents(before, treaty)
            if allocated == 0:
                continue
            claim_allocated[key] = claim_allocated.get(key, 0) + allocated
            bucket = treaty_totals.setdefault(
                (treaty.name, currency),
                {
                    "treaty": treaty.name,
                    "currency": currency,
                    "layer_currency": treaty.currency,
                    "allocated_cents": 0,
                    "movement_count": 0,
                },
            )
            bucket["allocated_cents"] += allocated
            bucket["movement_count"] += 1
            rows.append(
                {
                    "claim": claim,
                    "movement": movement.movement,
                    "currency": currency,
                    "treaty": treaty.name,
                    "layer_currency": treaty.currency,
                    "allocated_cents": allocated,
                    "allocated": cents_to_decimal(allocated),
                    "claim_cumulative_cents": after,
                    "claim_cumulative": cents_to_decimal(after),
                    "sources": list(movement.source_indices),
                }
            )

    claims: List[dict] = []
    for (claim, currency) in sorted(cumulative):
        net = cumulative[(claim, currency)]
        allocated = claim_allocated.get((claim, currency), 0)
        unallocated = net - allocated
        claims.append(
            {
                "claim": claim,
                "currency": currency,
                "cumulative_cents": net,
                "cumulative": cents_to_decimal(net),
                "allocated_cents": allocated,
                "allocated": cents_to_decimal(allocated),
                "unallocated_cents": unallocated,
                "unallocated": cents_to_decimal(unallocated),
            }
        )

    totals: List[dict] = []
    for currency in sorted(currency_totals):
        net = currency_totals[currency]
        allocated = sum(c["allocated_cents"] for c in claims if c["currency"] == currency)
        totals.append(
            {
                "currency": currency,
                "net_movements_cents": net,
                "net_movements": cents_to_decimal(net),
                "allocated_cents": allocated,
                "allocated": cents_to_decimal(allocated),
                "unallocated_cents": net - allocated,
                "unallocated": cents_to_decimal(net - allocated),
            }
        )

    treaty_rows = []
    for (name, currency) in sorted(treaty_totals):
        bucket = treaty_totals[(name, currency)]
        treaty_rows.append(
            {
                **bucket,
                "allocated": cents_to_decimal(bucket["allocated_cents"]),
            }
        )

    rows.sort(key=lambda r: (r["claim"], r["currency"], r["movement"], r["treaty"]))

    return {
        "allocations": rows,
        "claims": claims,
        "currency_totals": totals,
        "treaty_totals": treaty_rows,
        "replay_skips": replay_skips,
        "counts": {
            "applied_movements": len(ordered),
            "allocation_rows": len(rows),
        },
    }
