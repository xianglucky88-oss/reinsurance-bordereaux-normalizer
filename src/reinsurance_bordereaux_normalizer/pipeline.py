"""Orchestration boundary.

The pipeline is the only module that knows about both the parse and report
layers.  Its failure contract:

* a structural payload/treaty error yields ``status: "failed"`` with a report
  that still contains the audit trail of what was rejected;
* row-level errors yield ``status: "partial"``; valid movements are still
  normalized and allocated;
* normal operation yields ``status: "ok"``.

Fault injection exists so the QA harness can prove that the boundary fails
closed rather than silently emitting "ok".  It is opt-in, keyword-only, and is
never enabled by the CLI.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .engine import DuplicateConflictError, calculate


class _Fault(Exception):
    """Raised by injected faults in the pipeline boundary."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"injected fault at {stage}")


def _empty_data() -> dict:
    """The published data envelope for a run that allocated nothing."""
    return {
        "allocations": [],
        "claims": [],
        "currency_totals": [],
        "treaty_totals": [],
        "replay_skips": [],
        "counts": {"applied_movements": 0, "allocation_rows": 0},
    }


def run(
    payload: dict,
    *,
    fault: Optional[str] = None,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run the full normalization pipeline.

    ``fault`` / ``fault_hook`` exist solely for fault-injection tests and are
    documented as such; production callers never pass them.
    """
    from .parser import parse
    from .report import PROJECT, build_audit, build_result

    def checkpoint(stage: str) -> None:
        if fault_hook is not None:
            fault_hook(stage)
        if fault == stage:
            raise _Fault(stage)

    checkpoint("parse")
    parsed = parse(payload)

    if parsed.fatal_errors:
        data = _empty_data()
        # Nothing reached the engine: the engine-applied ledger is empty even
        # though the parser still reports which rows it validated.
        audit = build_audit(parsed, data, parsed.treaties, engine_applied=[])
        return build_result(
            PROJECT,
            data,
            status="failed",
            errors=parsed.errors,
            warnings=parsed.warnings,
            fatal_errors=parsed.fatal_errors,
            audit=audit,
        )

    checkpoint("engine")
    try:
        engine_result = calculate(parsed.movements, parsed.treaties)
    except DuplicateConflictError as exc:
        # Should be unreachable through the parser, but the boundary must fail
        # closed if the engine is ever called with un-collapsed input.
        fatal = [
            {
                "code": "movement_conflict",
                "message": str(exc),
                "source": "engine",
                "index": None,
            }
        ]
        data = _empty_data()
        audit = build_audit(parsed, data, parsed.treaties, engine_applied=[])
        return build_result(
            PROJECT,
            data,
            status="failed",
            errors=parsed.errors,
            warnings=parsed.warnings,
            fatal_errors=fatal,
            audit=audit,
        )

    checkpoint("report")
    # Use the engine's applied movement set, not just rows that produced an
    # allocation (a movement wholly below attachment produces no row).
    applied_keys = [(movement.claim, movement.movement) for movement in parsed.movements]
    audit = build_audit(parsed, engine_result, parsed.treaties, engine_applied=applied_keys)
    if not audit["conservation"]["all_checks_pass"]:
        fatal = [
            {
                "code": "conservation_failure",
                "message": "normalized net loss does not reconcile to layer allocations",
                "source": "audit",
                "index": None,
            }
        ]
        # A run whose reconciliation failed must not publish usable
        # allocations; the audited engine output stays in ``audit`` for
        # diagnosis only.
        return build_result(
            PROJECT,
            _empty_data(),
            status="failed",
            errors=parsed.errors,
            warnings=parsed.warnings,
            fatal_errors=fatal,
            audit=audit,
        )

    return build_result(
        PROJECT,
        engine_result,
        status="ok",
        errors=parsed.errors,
        warnings=parsed.warnings,
        fatal_errors=(),
        audit=audit,
    )
