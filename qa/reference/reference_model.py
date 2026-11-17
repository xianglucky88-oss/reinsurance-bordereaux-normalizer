"""An independent executable reference model.

This module deliberately imports **nothing** from
``reinsurance_bordereaux_normalizer``.  It is a second implementation of the
written settlement specification, used to cross-check the production code.
Where the production code uses ``decimal.Decimal``, this model parses the
decimal literal itself and works in :class:`fractions.Fraction`, so a shared
rounding bug cannot hide.

Reference specification
-----------------------
1. Loss rows must be objects with ``claim``, ``movement``, ``amount``,
   ``currency``.  Missing, non-finite, unparseable, or out-of-range amounts are
   rejected at row level and never contribute to any total.
2. Amounts are rounded half away from zero to two decimal places.  ``-0.00``
   normalizes to ``0.00``.
3. A movement key is ``(claim, movement)``.  Identical normalized repeats are
   replays and count once.  A key repeated with a different normalized
   ``(amount, currency)`` is a conflict; the whole key is excluded.
4. Running cumulative loss is tracked per ``(claim, currency)``.  No FX
   conversion ever happens.
5. A treaty covers ``clamp(position - attachment, 0, limit)``.  A treaty with a
   ``currency`` only applies to movements in that currency; a treaty without one
   applies to every currency.
6. Movement allocations are attributed in canonical ``(claim, currency,
   movement)`` order, so the input order is not observable.
7. Every movement's layer allocation is the change in coverage caused by that
   movement.  Negative movements therefore produce non-positive allocations and
   can claw back earlier layer amounts.
8. Configurations with duplicate names, negative attachment, non-positive
   limit, or overlapping layers (per currency tower) are fatal.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_ABS = Fraction(10**12)  # one trillion, matching the production bound


class Rejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_decimal(text: str) -> Fraction:
    """Parse ``[+-]digits[.digits]`` without using Decimal or float."""
    if not isinstance(text, str):
        raise Rejected("invalid_amount", "amount must be text")
    stripped = text.strip()
    if not stripped:
        raise Rejected("invalid_amount", "amount is empty")
    sign = 1
    if stripped[0] in "+-":
        if stripped[0] == "-":
            sign = -1
        stripped = stripped[1:]
    if not stripped:
        raise Rejected("invalid_amount", "amount has no digits")
    if "e" in stripped.lower():
        raise Rejected("invalid_amount", "exponent notation is not accepted")
    parts = stripped.split(".")
    if len(parts) > 2:
        raise Rejected("invalid_amount", "amount has multiple decimal points")
    whole = parts[0]
    frac = parts[1] if len(parts) == 2 else ""
    if not whole and not frac:
        raise Rejected("invalid_amount", "amount has no digits")
    if whole and not whole.isdigit():
        raise Rejected("invalid_amount", "amount has non-digit characters")
    if frac and not frac.isdigit():
        raise Rejected("invalid_amount", "amount has non-digit characters")
    value = Fraction(int(whole or "0")) + (
        Fraction(int(frac)) / (10 ** len(frac)) if frac else Fraction(0)
    )
    return sign * value


def round_half_away(value: Fraction) -> int:
    """Round a Fraction to integer cents, half away from zero."""
    scaled = value * 100
    sign = -1 if scaled < 0 else 1
    magnitude = abs(scaled)
    quotient, remainder = divmod(magnitude.numerator, magnitude.denominator)
    if Fraction(remainder * 2) >= magnitude.denominator:
        quotient += 1
    return sign * quotient


def amount_cents(value: Any) -> int:
    """Normalize a reported amount to integer cents."""
    if value is None or isinstance(value, bool):
        raise Rejected("invalid_amount", "amount must be a finite decimal number")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise Rejected("invalid_amount", "amount must be finite")
        text = repr(value)
    elif isinstance(value, str):
        text = value
    else:
        raise Rejected("invalid_amount", "amount must be a number or decimal string")
    fraction = _parse_decimal(text)
    if abs(fraction) > MAX_ABS:
        raise Rejected("amount_out_of_range", "amount exceeds the supported range")
    return round_half_away(fraction)


def currency_code(value: Any) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise Rejected("invalid_currency", "currency must be a three-letter code")
    text = value.strip().upper()
    if len(text) != 3 or not all("A" <= character <= "Z" for character in text):
        raise Rejected("invalid_currency", "currency must be a three-letter code")
    return text


def identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise Rejected(f"invalid_{field}", f"{field} must be a string or integer")
    text = str(value).strip()
    if not text:
        raise Rejected(f"invalid_{field}", f"{field} must not be empty")
    if len(text) > 200:
        raise Rejected(f"invalid_{field}", f"{field} is too long")
    return text


def cents_text(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


class ReferenceResult:
    def __init__(self) -> None:
        self.status: str = "ok"
        self.fatal: List[dict] = []
        self.errors: List[dict] = []
        self.warnings: List[dict] = []
        self.applied: List[dict] = []      # normalized movements, canonical order
        self.replays: List[dict] = []
        self.allocations: List[dict] = []  # per movement/treaty rows
        self.claims: List[dict] = []
        self.currency_totals: List[dict] = []
        self.layer_totals: List[dict] = []
        self.rejected_rows: int = 0
        self.conflict_keys: int = 0

    @property
    def net_by_currency(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for row in self.applied:
            result[row["currency"]] = result.get(row["currency"], 0) + row["amount_cents"]
        return result

    @property
    def allocated_by_currency(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for row in self.allocations:
            result[row["currency"]] = result.get(row["currency"], 0) + row["allocated_cents"]
        return result


def _treaties(payload: dict, result: ReferenceResult) -> List[dict]:
    raw = payload.get("treaties", [])
    if raw is None and "treaties" not in payload:
        return []
    if not isinstance(raw, list):
        result.fatal.append({"code": "invalid_treaties", "source": "treaties", "index": None})
        return []

    treaties: List[dict] = []
    names: Dict[str, int] = {}
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            result.fatal.append(
                {"code": "invalid_treaty", "source": f"treaties[{index}]", "index": index}
            )
            continue
        try:
            name = identifier(row.get("name"), "name")
            attachment = amount_cents(row.get("attachment"))
            limit = amount_cents(row.get("limit"))
            currency = row.get("currency")
            currency = None if currency is None else currency_code(currency)
        except Rejected as exc:
            result.fatal.append(
                {"code": exc.code, "source": f"treaties[{index}]", "index": index}
            )
            continue
        if attachment < 0:
            result.fatal.append(
                {"code": "invalid_attachment", "source": f"treaties[{index}]", "index": index}
            )
            continue
        if limit <= 0:
            result.fatal.append(
                {"code": "invalid_limit", "source": f"treaties[{index}]", "index": index}
            )
            continue
        if name in names:
            result.fatal.append(
                {"code": "duplicate_treaty", "source": f"treaties[{index}]", "index": index}
            )
            continue
        names[name] = index
        treaties.append(
            {
                "name": name,
                "attachment": attachment,
                "limit": limit,
                "currency": currency,
                "source_index": index,
            }
        )
    return treaties


def _overlap_check(
    treaties: Sequence[dict], currencies: Sequence[str]
) -> List[dict]:
    fatal: List[dict] = []
    checked = set(currencies)
    checked.update(t["currency"] for t in treaties if t["currency"] is not None)
    towers = sorted(checked) if checked else ["<none>"]
    for currency in towers:
        applicable = sorted(
            [t for t in treaties if t["currency"] is None or t["currency"] == currency],
            key=lambda t: (t["attachment"], t["limit"], t["name"], t["source_index"]),
        )
        for previous, current in zip(applicable, applicable[1:]):
            if current["attachment"] < previous["attachment"] + previous["limit"]:
                fatal.append(
                    {
                        "code": "treaty_overlap",
                        "source": f"treaties[{current['source_index']}]",
                        "index": current["source_index"],
                    }
                )
    return fatal


def _loss_rows(payload: dict, result: ReferenceResult) -> List[dict]:
    raw = payload.get("losses", [])
    if raw is None and "losses" not in payload:
        return []
    if not isinstance(raw, list):
        result.fatal.append({"code": "invalid_losses", "source": "losses", "index": None})
        return []

    rows: List[dict] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            result.errors.append(
                {"code": "invalid_loss_row", "source": f"losses[{index}]", "index": index}
            )
            result.rejected_rows += 1
            continue
        missing = [
            field
            for field in ("claim", "movement", "amount", "currency")
            if row.get(field) is None
        ]
        if missing:
            result.errors.append(
                {"code": "missing_field", "source": f"losses[{index}]", "index": index}
            )
            result.rejected_rows += 1
            continue
        try:
            claim = identifier(row.get("claim"), "claim")
            movement = identifier(row.get("movement"), "movement")
            amount = amount_cents(row.get("amount"))
            currency = currency_code(row.get("currency"))
        except Rejected as exc:
            result.errors.append(
                {"code": exc.code, "source": f"losses[{index}]", "index": index}
            )
            result.rejected_rows += 1
            continue
        rows.append(
            {
                "claim": claim,
                "movement": movement,
                "currency": currency,
                "amount_cents": amount,
                "source_index": index,
            }
        )
    return rows


def _consolidate(rows: Sequence[dict], result: ReferenceResult) -> List[dict]:
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for row in rows:
        groups.setdefault((row["claim"], row["movement"]), []).append(row)

    movements: List[dict] = []
    for (claim, movement), members in sorted(groups.items()):
        values = sorted({(m["amount_cents"], m["currency"]) for m in members})
        if len(values) > 1:
            result.errors.append(
                {
                    "code": "movement_conflict",
                    "source": f"losses[{min(m['source_index'] for m in members)}]",
                    "index": min(m["source_index"] for m in members),
                    "claim": claim,
                    "movement": movement,
                }
            )
            result.conflict_keys += 1
            continue
        amount, currency = values[0]
        movements.append(
            {
                "claim": claim,
                "movement": movement,
                "currency": currency,
                "amount_cents": amount,
                "sources": sorted(m["source_index"] for m in members),
            }
        )
        if len(members) > 1:
            result.replays.append({"claim": claim, "movement": movement})
    return movements


def _coverage(position: int, treaty: dict) -> int:
    return max(0, min(position, treaty["attachment"] + treaty["limit"]) - treaty["attachment"])


def evaluate(payload: Any) -> ReferenceResult:
    result = ReferenceResult()
    if not isinstance(payload, dict):
        result.fatal.append({"code": "invalid_payload", "source": "payload", "index": None})
        result.status = "failed"
        return result

    treaties = _treaties(payload, result)
    rows = _loss_rows(payload, result)
    movements = _consolidate(rows, result)
    result.applied = sorted(
        movements, key=lambda m: (m["claim"], m["currency"], m["movement"], m["sources"])
    )
    result.fatal.extend(_overlap_check(treaties, sorted({m["currency"] for m in movements})))

    if result.fatal:
        result.status = "failed"
        return result

    cumulative: Dict[Tuple[str, str], int] = {}
    allocated_claims: Dict[Tuple[str, str], int] = {}
    layer_totals: Dict[Tuple[str, str], dict] = {}

    for movement in result.applied:
        claim = movement["claim"]
        currency = movement["currency"]
        key = (claim, currency)
        before = cumulative.get(key, 0)
        after = before + movement["amount_cents"]
        cumulative[key] = after
        applicable = sorted(
            [
                t
                for t in treaties
                if t["currency"] is None or t["currency"] == currency
            ],
            key=lambda t: (t["attachment"], t["limit"], t["name"], t["source_index"]),
        )
        for treaty in applicable:
            allocated = _coverage(after, treaty) - _coverage(before, treaty)
            if allocated == 0:
                continue
            allocated_claims[key] = allocated_claims.get(key, 0) + allocated
            bucket = layer_totals.setdefault(
                (treaty["name"], currency),
                {"treaty": treaty["name"], "currency": currency, "allocated_cents": 0},
            )
            bucket["allocated_cents"] += allocated
            result.allocations.append(
                {
                    "claim": claim,
                    "movement": movement["movement"],
                    "currency": currency,
                    "treaty": treaty["name"],
                    "allocated_cents": allocated,
                    "claim_cumulative_cents": after,
                    "sources": list(movement["sources"]),
                }
            )

    for key in sorted(cumulative):
        claim, currency = key
        net = cumulative[key]
        allocated = allocated_claims.get(key, 0)
        result.claims.append(
            {
                "claim": claim,
                "currency": currency,
                "cumulative_cents": net,
                "allocated_cents": allocated,
                "unallocated_cents": net - allocated,
            }
        )

    nets = result.net_by_currency
    allocated = result.allocated_by_currency
    for currency in sorted(set(nets) | set(allocated)):
        result.currency_totals.append(
            {
                "currency": currency,
                "net_cents": nets.get(currency, 0),
                "allocated_cents": allocated.get(currency, 0),
                "unallocated_cents": nets.get(currency, 0) - allocated.get(currency, 0),
            }
        )

    for key in sorted(layer_totals):
        result.layer_totals.append(layer_totals[key])

    if result.errors or result.conflict_keys:
        result.status = "partial"
    else:
        result.status = "ok"
    return result
