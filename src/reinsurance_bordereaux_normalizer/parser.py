"""Input validation and record extraction.

The parser is the only place that turns reported values into integer cents.
Money is never represented as a binary float internally.  Every input row ends
up in a traceable ledger entry, including rows that were rejected.

Normalization contract (see README for the full specification):

* amounts are parsed as decimal text, must be finite, and are rounded
  half-up (away from zero) to two decimal places;
* a movement is identified by ``(claim, movement)``;
* byte-identical *normalized* repetitions are replays and are counted once;
* a key reported with two different normalized values is a conflict and the
  whole key is excluded deterministically;
* currencies are validated, upper-cased, and never converted.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Movement, ParseResult, Treaty

AMOUNT_QUANTUM = Decimal("0.01")
MAX_ABS_AMOUNT = Decimal("1000000000000.00")
_AMOUNT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
_MAX_IDENTIFIER_LEN = 200


class FieldError(ValueError):
    """A single field failed validation.

    ``code`` is stable and machine-readable; ``message`` is human-readable.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def amount_to_cents(value: Any) -> int:
    """Convert a reported amount to integer cents, half-up.

    Raises :class:`FieldError` for invalid, non-finite, or out-of-range values.
    """
    if _is_bool(value) or value is None:
        raise FieldError("invalid_amount", "amount must be a finite decimal number")

    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise FieldError("invalid_amount", "amount must be finite")
        decimal_value = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not _AMOUNT_RE.match(text):
            raise FieldError("invalid_amount", "amount is not a plain decimal number")
        try:
            decimal_value = Decimal(text)
        except InvalidOperation:
            raise FieldError("invalid_amount", "amount is not a valid decimal number")
    else:
        raise FieldError("invalid_amount", "amount must be a number or decimal string")

    if not decimal_value.is_finite():
        raise FieldError("invalid_amount", "amount must be finite")
    if abs(decimal_value) > MAX_ABS_AMOUNT:
        raise FieldError("amount_out_of_range", "amount exceeds the supported range")

    try:
        rounded = decimal_value.quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise FieldError("invalid_amount", "amount cannot be normalized to cents")

    cents = int(rounded.scaleb(2))
    return 0 if cents == 0 else cents


def cents_to_text(cents: int) -> str:
    """Render integer cents as a fixed two-decimal string (``-0.00`` -> ``0.00``)."""
    sign = "-" if cents < 0 else ""
    absolute = abs(int(cents))
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def normalize_currency(value: Any) -> str:
    if not isinstance(value, str) or _is_bool(value):
        raise FieldError("invalid_currency", "currency must be a three-letter code")
    text = value.strip().upper()
    if not _CURRENCY_RE.match(text):
        raise FieldError("invalid_currency", "currency must be a three-letter code")
    return text


def normalize_identifier(value: Any, field: str) -> str:
    if _is_bool(value) or not isinstance(value, (str, int)):
        raise FieldError(f"invalid_{field}", f"{field} must be a string or integer")
    text = str(value).strip()
    if not text:
        raise FieldError(f"invalid_{field}", f"{field} must not be empty")
    if len(text) > _MAX_IDENTIFIER_LEN:
        raise FieldError(f"invalid_{field}", f"{field} exceeds {_MAX_IDENTIFIER_LEN} characters")
    return text


def _error(code: str, message: str, source: str, index: Optional[int], **extra: Any) -> dict:
    payload: Dict[str, Any] = {"code": code, "message": message, "source": source, "index": index}
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _parse_treaties(payload: dict) -> Tuple[List[Treaty], List[dict], List[dict], List[dict]]:
    """Return ``(treaties, fatal_errors, errors, warnings)`` for the treaty block."""
    fatal: List[dict] = []
    errors: List[dict] = []
    warnings: List[dict] = []

    raw_treaties = payload.get("treaties", None)
    if raw_treaties is None and "treaties" not in payload:
        raw_treaties = []
    if not isinstance(raw_treaties, list):
        fatal.append(_error("invalid_treaties", "treaties must be a list", "treaties", None))
        return [], fatal, errors, warnings

    treaties: List[Treaty] = []
    seen_names: Dict[str, int] = {}
    for index, raw in enumerate(raw_treaties):
        source = f"treaties[{index}]"
        if not isinstance(raw, dict):
            fatal.append(_error("invalid_treaty", "treaty must be an object", source, index))
            continue
        try:
            name = normalize_identifier(raw.get("name"), "name")
            attachment_cents = amount_to_cents(raw.get("attachment"))
            limit_cents = amount_to_cents(raw.get("limit"))
            currency = raw.get("currency", None)
            currency_norm = None if currency is None else normalize_currency(currency)
        except FieldError as exc:
            fatal.append(_error(exc.code, exc.message, source, index, treaty=raw.get("name")))
            continue

        if attachment_cents < 0:
            fatal.append(
                _error("invalid_attachment", "attachment must not be negative", source, index, treaty=name)
            )
            continue
        if limit_cents <= 0:
            fatal.append(_error("invalid_limit", "limit must be positive", source, index, treaty=name))
            continue
        if name in seen_names:
            fatal.append(
                _error(
                    "duplicate_treaty",
                    f"treaty name is not unique (first seen at index {seen_names[name]})",
                    source,
                    index,
                    treaty=name,
                )
            )
            continue
        seen_names[name] = index
        treaties.append(
            Treaty(
                name=name,
                attachment=Decimal(attachment_cents).scaleb(-2),
                limit=Decimal(limit_cents).scaleb(-2),
                currency=currency_norm,
                source_index=index,
            )
        )
    return treaties, fatal, errors, warnings


def _check_overlaps(
    treaties: Sequence[Treaty], movement_currencies: Iterable[str]
) -> List[dict]:
    """Reject overlapping layers per currency tower.

    Overlapping layers would double-allocate the same loss and destroy the
    conservation identity, so this is treated as a fatal configuration error.
    """
    fatal: List[dict] = []
    currencies = set(movement_currencies)
    currencies.update(t.currency for t in treaties if t.currency is not None)
    if not currencies:
        currencies.add("__ANY__")

    for currency in sorted(currencies):
        applicable = [
            t
            for t in treaties
            if t.currency is None or t.currency == currency
        ]
        ordered = sorted(applicable, key=lambda t: (t.attachment, t.limit, t.name, t.source_index))
        for previous, current in zip(ordered, ordered[1:]):
            previous_top = previous.attachment + previous.limit
            if current.attachment < previous_top:
                fatal.append(
                    _error(
                        "treaty_overlap",
                        (
                            f"treaty {current.name!r} attaches at {current.attachment} before "
                            f"treaty {previous.name!r} exhausts at {previous_top}"
                        ),
                        f"treaties[{current.source_index}]",
                        current.source_index,
                        treaty=current.name,
                        currency=None if currency == "__ANY__" else currency,
                    )
                )
    return fatal


def _parse_losses(rows: Sequence[Any]) -> Tuple[List[dict], List[dict], int]:
    """Return ``(valid_rows, errors, input_count)``.

    ``valid_rows`` carry normalized values and their source index.
    """
    valid: List[dict] = []
    errors: List[dict] = []
    for index, raw in enumerate(rows):
        source = f"losses[{index}]"
        if not isinstance(raw, dict):
            errors.append(
                _error("invalid_loss_row", "loss must be an object", source, index)
            )
            continue

        claim_value = raw.get("claim")
        movement_value = raw.get("movement")
        missing = [
            field
            for field, present in (
                ("claim", claim_value is not None),
                ("movement", movement_value is not None),
                ("amount", "amount" in raw and raw.get("amount") is not None),
                ("currency", "currency" in raw and raw.get("currency") is not None),
            )
            if not present
        ]
        if missing:
            errors.append(
                _error(
                    "missing_field",
                    "missing required field(s): " + ", ".join(missing),
                    source,
                    index,
                    claim=claim_value if isinstance(claim_value, (str, int)) else None,
                )
            )
            continue

        try:
            claim = normalize_identifier(claim_value, "claim")
            movement = normalize_identifier(movement_value, "movement")
            currency = normalize_currency(raw.get("currency"))
            amount_cents = amount_to_cents(raw.get("amount"))
        except FieldError as exc:
            errors.append(
                _error(
                    exc.code,
                    exc.message,
                    source,
                    index,
                    claim=claim_value if isinstance(claim_value, str) else None,
                    movement=movement_value if isinstance(movement_value, str) else None,
                )
            )
            continue

        valid.append(
            {
                "claim": claim,
                "movement": movement,
                "currency": currency,
                "amount_cents": amount_cents,
                "raw_amount": raw.get("amount"),
                "source_index": index,
            }
        )
    return valid, errors, len(rows)


def _group_movements(
    valid_rows: Sequence[dict], errors: List[dict], warnings: List[dict]
) -> Tuple[List[Movement], List[dict]]:
    """Apply replay/conflict semantics and build the traceable ledger."""
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for row in valid_rows:
        groups.setdefault((row["claim"], row["movement"]), []).append(row)

    movements: List[Movement] = []
    ledger: List[dict] = []

    for (claim, movement), rows in sorted(groups.items()):
        distinct = sorted(
            {(row["amount_cents"], row["currency"]) for row in rows}
        )
        source_indices = sorted(row["source_index"] for row in rows)
        if len(distinct) > 1:
            errors.append(
                _error(
                    "movement_conflict",
                    "movement was reported with conflicting values; the key is excluded",
                    f"losses[{source_indices[0]}]",
                    source_indices[0],
                    claim=claim,
                    movement=movement,
                    values=[
                        {"amount": cents_to_text(cents), "currency": currency}
                        for cents, currency in distinct
                    ],
                )
            )
            ledger.append(
                {
                    "claim": claim,
                    "movement": movement,
                    "currency": None,
                    "amount": None,
                    "amount_cents": None,
                    "status": "conflict",
                    "code": "movement_conflict",
                    "source_indices": source_indices,
                }
            )
            continue

        amount_cents, currency = distinct[0]
        movements.append(
            Movement(
                claim=claim,
                movement=movement,
                amount=Decimal(amount_cents).scaleb(-2),
                currency=currency,
                raw_amount=str(rows[0]["raw_amount"]),
                source_indices=tuple(source_indices),
            )
        )
        ledger.append(
            {
                "claim": claim,
                "movement": movement,
                "currency": currency,
                "amount": cents_to_text(amount_cents),
                "amount_cents": amount_cents,
                "status": "applied" if len(rows) == 1 else "replayed",
                "code": None,
                "replay_count": len(rows) - 1,
                "source_indices": source_indices,
            }
        )

    # Mixed-currency claims are legitimate but cannot be netted without FX.
    by_claim: Dict[str, set] = {}
    for movement in movements:
        by_claim.setdefault(movement.claim, set()).add(movement.currency)
    for claim in sorted(by_claim):
        currencies = sorted(by_claim[claim])
        if len(currencies) > 1:
            warnings.append(
                _error(
                    "mixed_currency_claim",
                    "claim spans multiple currencies; each currency is normalized separately",
                    f"claim:{claim}",
                    None,
                    claim=claim,
                    currencies=currencies,
                )
            )

    ledger.sort(key=lambda entry: entry["source_indices"][0])
    return movements, ledger


def load_records(payload: dict) -> list:
    """Backward-compatible row loader; structural validation lives in :func:`parse`."""
    rows = payload.get("losses", [])
    if not isinstance(rows, list):
        raise ValueError("losses must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]


def parse(payload: Any) -> ParseResult:
    """Validate and normalize a payload without mutating it."""
    if not isinstance(payload, dict):
        fatal = [_error("invalid_payload", "payload must be a JSON object", "payload", None)]
        return ParseResult(
            movements=(),
            treaties=(),
            errors=(),
            warnings=(),
            fatal_errors=tuple(fatal),
            ledger=(),
            input_count=0,
            rejected_rows=0,
            conflict_count=0,
            replay_count=0,
        )

    errors: List[dict] = []
    warnings: List[dict] = []

    treaties, treaty_fatal, treaty_errors, treaty_warnings = _parse_treaties(payload)
    errors.extend(treaty_errors)
    warnings.extend(treaty_warnings)

    rows = payload.get("losses", None)
    if rows is None and "losses" not in payload:
        rows = []
    if not isinstance(rows, list):
        treaty_fatal.append(
            _error("invalid_losses", "losses must be a list", "losses", None)
        )
        rows = []

    valid_rows, loss_errors, input_count = _parse_losses(rows)
    errors.extend(loss_errors)
    movements, ledger = _group_movements(valid_rows, errors, warnings)

    fatal = list(treaty_fatal)
    fatal.extend(_check_overlaps(treaties, {m.currency for m in movements}))

    # Rejected rows are part of the audit ledger too.
    for error in loss_errors:
        if error["source"].startswith("losses["):
            index = error["index"]
            if index is not None:
                ledger.append(
                    {
                        "claim": error.get("claim"),
                        "movement": error.get("movement"),
                        "currency": None,
                        "amount": None,
                        "amount_cents": None,
                        "status": "rejected",
                        "code": error["code"],
                        "source_indices": [index],
                    }
                )
    ledger.sort(key=lambda entry: entry["source_indices"][0])

    rejected = sum(1 for entry in ledger if entry["status"] == "rejected")
    conflicts = sum(1 for entry in ledger if entry["status"] == "conflict")
    replays = sum(int(entry.get("replay_count", 0)) for entry in ledger)

    def deterministic_key(item: dict) -> Tuple[str, str, str]:
        return (str(item.get("source", "")), str(item.get("code", "")), str(item.get("message", "")))

    errors.sort(key=deterministic_key)
    warnings.sort(key=deterministic_key)
    fatal.sort(key=deterministic_key)

    return ParseResult(
        movements=tuple(movements),
        treaties=tuple(treaties),
        errors=tuple(errors),
        warnings=tuple(warnings),
        fatal_errors=tuple(fatal),
        ledger=tuple(ledger),
        input_count=input_count,
        rejected_rows=rejected,
        conflict_count=conflicts,
        replay_count=replays,
    )
