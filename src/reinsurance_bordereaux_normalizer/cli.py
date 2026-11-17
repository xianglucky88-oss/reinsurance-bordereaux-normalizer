"""Deterministic JSON CLI.

Exit codes are part of the contract and are relied on by the QA harness:

* ``0`` -- ``status: "ok"``
* ``1`` -- ``status: "partial"`` (some rows rejected, valid rows processed)
* ``2`` -- ``status: "failed"`` (fatal configuration/payload error)
* ``3`` -- the input file could not be read or was not valid JSON

The JSON envelope is always printed to stdout, even on a fatal input error, so
that a caller can audit the failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2
EXIT_UNREADABLE = 3


def _fatal_envelope(message: str) -> dict:
    return {
        "project": "reinsurance-bordereaux-normalizer",
        "status": "failed",
        "fatal_errors": [
            {
                "code": "INVALID_JSON",
                "message": message,
                "source": "input",
                "index": None,
            }
        ],
        "allocations": [],
        "claims": [],
        "currency_totals": [],
        "treaty_totals": [],
        "replay_skips": [],
        "counts": {"applied_movements": 0, "allocation_rows": 0},
        "errors": [],
        "warnings": [],
    }


def _exit_code(status: str) -> int:
    if status == "ok":
        return EXIT_OK
    if status == "partial":
        return EXIT_PARTIAL
    return EXIT_FAILED


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize loss movements and allocate them across treaty layers."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(Path(__file__).parents[2] / "fixtures" / "input.json"),
    )
    args = parser.parse_args()

    try:
        payload = json.loads(Path(args.input).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps(_fatal_envelope(str(exc)), ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_UNREADABLE

    result = run(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
