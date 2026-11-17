"""Immutable contracts shared by the parser, engine, and report layers."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Treaty:
    """A treaty layer normalized to cents.

    Coverage is ``clamp(x - attachment, 0, limit)``.  The attachment point
    itself has zero covered loss; the top of the layer is fully covered.
    """

    name: str
    attachment: Decimal
    limit: Decimal
    currency: Optional[str]
    source_index: int


@dataclass(frozen=True)
class Movement:
    """A single, field-validated loss movement.

    ``amount`` is already rounded half-up to two decimal places.  ``raw_amount``
    and ``source_indices`` retain the reported values for the audit trail.
    """

    claim: str
    movement: str
    amount: Decimal
    currency: str
    raw_amount: str
    source_indices: Tuple[int, ...]


@dataclass(frozen=True)
class ParseResult:
    movements: Tuple[Movement, ...]
    treaties: Tuple[Treaty, ...]
    errors: Tuple[dict[str, Any], ...]
    warnings: Tuple[dict[str, Any], ...]
    fatal_errors: Tuple[dict[str, Any], ...]
    ledger: Tuple[dict[str, Any], ...]
    input_count: int
    rejected_rows: int
    conflict_count: int
    replay_count: int
