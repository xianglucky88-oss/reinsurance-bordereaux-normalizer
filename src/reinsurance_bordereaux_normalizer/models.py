"""Domain model: immutable value types, decimal money, and period arithmetic.

All monetary values are ``decimal.Decimal`` quantized to ``MONEY_SCALE`` (2dp,
ROUND_HALF_UP) at every system boundary. FX rates are quantized to
``RATE_SCALE`` (6dp). Nothing here performs I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

MONEY_SCALE = Decimal("0.01")
RATE_SCALE = Decimal("0.000001")
ROUNDING = ROUND_HALF_UP
ZERO = Decimal("0.00")

MOVEMENT_TYPES = ("ADDITION", "WITHDRAWAL", "CORRECTION", "RECOVERY")

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CCY_RE = re.compile(r"^[A-Z]{3}$")


class DomainError(ValueError):
    """Validation failure that must be surfaced to the caller, never crash."""


# ---------------------------------------------------------------------------
# periods
# ---------------------------------------------------------------------------

def is_period(value: Any) -> bool:
    return isinstance(value, str) and bool(_PERIOD_RE.match(value))


def next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def prev_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def periods_between(first: str, last: str) -> list[str]:
    """Inclusive monthly range; period strings sort chronologically."""
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = next_period(cur)
    return out


# ---------------------------------------------------------------------------
# decimals
# ---------------------------------------------------------------------------

def to_decimal(value: Any, field_name: str = "value") -> Decimal:
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        raise DomainError(f"{field_name}: boolean is not a number")
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise DomainError(f"{field_name}: not a number: {value!r}")
    else:
        raise DomainError(f"{field_name}: unsupported type {type(value).__name__}")
    if not d.is_finite():
        raise DomainError(f"{field_name}: non-finite number")
    return d


def q_money(d: Decimal) -> Decimal:
    return d.quantize(MONEY_SCALE, rounding=ROUNDING)


def q_rate(d: Decimal) -> Decimal:
    return d.quantize(RATE_SCALE, rounding=ROUNDING)


def money_str(value: Any) -> str:
    d = q_money(to_decimal(value))
    if d == 0:
        d = abs(d)  # avoid "-0.00"
    return f"{d:.2f}"


def rate_str(value: Any) -> str:
    return f"{q_rate(to_decimal(value)):f}"


def is_currency(value: Any) -> bool:
    return isinstance(value, str) and bool(_CCY_RE.match(value))


# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layer:
    layer_id: str
    attachment: Decimal
    limit: Decimal
    reinstatements: int = 0
    reinstatement_premium_rate: Decimal = Decimal("0")

    @property
    def exhaustion(self) -> Decimal:
        return self.attachment + self.limit

    @property
    def aggregate_capacity(self) -> Decimal:
        """Per-claim limit times (1 + paid reinstatements)."""
        return self.limit * (1 + self.reinstatements)


@dataclass(frozen=True)
class TreatyVersion:
    program: str
    version: int
    effective_from: str            # first booking period covered (inclusive)
    effective_to: Optional[str]    # last booking period covered; None = open
    layers: tuple

    def covers(self, period: str) -> bool:
        return self.effective_from <= period and (
            self.effective_to is None or period <= self.effective_to
        )


@dataclass(frozen=True)
class TreatyProgram:
    program_id: str
    settlement_currency: str
    versions: tuple  # tuple[TreatyVersion, ...] sorted by effective_from

    def version_for(self, period: str) -> Optional[TreatyVersion]:
        for v in self.versions:
            if v.covers(period):
                return v
        return None

    def version_by_id(self, version: int) -> Optional[TreatyVersion]:
        for v in self.versions:
            if v.version == version:
                return v
        return None


@dataclass(frozen=True)
class FxSnapshot:
    """Immutable rate table for one booking period.

    ``rates[ccy]`` = units of ``base`` per 1 unit of ``ccy``. A revised table
    for the same period is a new ``revision``; it never alters conversions
    already frozen onto confirmed events.
    """
    snapshot_id: str
    period: str
    revision: int
    base: str
    rates: dict  # dict[str, Decimal]

    def rate_for(self, currency: str) -> Optional[Decimal]:
        if currency == self.base:
            return Decimal(1)
        return self.rates.get(currency)


@dataclass(frozen=True)
class Risk:
    risk_id: str
    treaty_program: str
    currency: str
    policy_id: str = ""
    cedent: str = ""


@dataclass(frozen=True)
class Claim:
    claim_id: str
    risk_id: str


# ---------------------------------------------------------------------------
# confirmed movement events (the source of truth)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MovementEvent:
    """A normalized, confirmed movement.

    ``contribution`` is the signed change to the claim's ground-up loss in
    settlement currency, frozen at confirmation time:

    - ADDITION:   +amount, converted at the booking period's FX snapshot
    - CORRECTION: +(new - previous effective amount) in claim currency,
                  converted at the correction's own frozen rate
    - WITHDRAWAL: exact negative of the target's frozen settlement
                  contributions (reversal at original rates, no FX drift)
    - RECOVERY:   -amount converted at the frozen rate; ``recovery`` carries
                  the positive settlement amount for recovery reporting
    """
    event_seq: int
    movement_id: str
    claim_id: str
    type: str
    amount: Decimal                 # as reported; CORRECTION: the new amount
    currency: str
    delta_amount: Decimal           # signed change in claim currency
    contribution: Decimal           # signed change in settlement currency
    recovery: Decimal               # settlement recovery (RECOVERY only)
    fx_snapshot_id: Optional[str]
    fx_revision: Optional[int]
    fx_rate: Optional[Decimal]
    booking_period: str
    occurrence_period: Optional[str]
    target_movement_id: Optional[str]
    batch_id: str
    source: str
    row: int

    def fingerprint(self) -> tuple:
        return movement_fingerprint(
            {
                "movement_id": self.movement_id,
                "claim_id": self.claim_id,
                "type": self.type,
                "amount": money_str(self.amount),
                "currency": self.currency,
                "target_movement_id": self.target_movement_id,
                "occurrence_period": self.occurrence_period,
            }
        )


def movement_fingerprint(raw: dict) -> tuple:
    """Natural-key content hash used for idempotent re-submission."""
    amount = raw.get("amount")
    return (
        str(raw.get("claim_id") or ""),
        str(raw.get("type") or ""),
        money_str(amount) if amount is not None else "0.00",
        str(raw.get("currency") or ""),
        str(raw.get("target_movement_id") or ""),
        str(raw.get("occurrence_period") or ""),
    )


def event_to_dict(e: MovementEvent) -> dict:
    return {
        "event_seq": e.event_seq,
        "movement_id": e.movement_id,
        "claim_id": e.claim_id,
        "type": e.type,
        "amount": money_str(e.amount),
        "currency": e.currency,
        "delta_amount": money_str(e.delta_amount),
        "contribution": money_str(e.contribution),
        "recovery": money_str(e.recovery),
        "fx_snapshot_id": e.fx_snapshot_id,
        "fx_revision": e.fx_revision,
        "fx_rate": rate_str(e.fx_rate) if e.fx_rate is not None else None,
        "booking_period": e.booking_period,
        "occurrence_period": e.occurrence_period,
        "target_movement_id": e.target_movement_id,
        "batch_id": e.batch_id,
        "source": e.source,
        "row": e.row,
    }


def event_from_dict(d: dict) -> MovementEvent:
    return MovementEvent(
        event_seq=int(d["event_seq"]),
        movement_id=d["movement_id"],
        claim_id=d["claim_id"],
        type=d["type"],
        amount=to_decimal(d["amount"], "amount"),
        currency=d["currency"],
        delta_amount=to_decimal(d["delta_amount"], "delta_amount"),
        contribution=to_decimal(d["contribution"], "contribution"),
        recovery=to_decimal(d["recovery"], "recovery"),
        fx_snapshot_id=d.get("fx_snapshot_id"),
        fx_revision=d.get("fx_revision"),
        fx_rate=to_decimal(d["fx_rate"], "fx_rate") if d.get("fx_rate") is not None else None,
        booking_period=d["booking_period"],
        occurrence_period=d.get("occurrence_period"),
        target_movement_id=d.get("target_movement_id"),
        batch_id=d["batch_id"],
        source=d["source"],
        row=int(d["row"]),
    )


# ---------------------------------------------------------------------------
# batch outcomes
# ---------------------------------------------------------------------------

@dataclass
class RowError:
    row: int
    identifier: str
    reason: str

    def as_dict(self) -> dict:
        return {"row": self.row, "identifier": self.identifier, "reason": self.reason}


@dataclass
class BatchResult:
    batch_id: Optional[str]
    source: Optional[str]
    period: Optional[str]
    status: str                    # confirmed | partial | rejected | duplicate
    received_rows: int = 0
    confirmed: int = 0
    duplicates: int = 0
    content_hash: str = ""
    reason: str = ""
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "source": self.source,
            "period": self.period,
            "status": self.status,
            "received_rows": self.received_rows,
            "confirmed": self.confirmed,
            "duplicates": self.duplicates,
            "content_hash": self.content_hash,
            "reason": self.reason,
            "errors": [e.as_dict() if isinstance(e, RowError) else e for e in self.errors],
        }


@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: dict
