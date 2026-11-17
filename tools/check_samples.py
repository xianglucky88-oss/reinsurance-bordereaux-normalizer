#!/usr/bin/env python3
"""Re-verify the handover sample corpus against the current engine.

Usage:
    python3 tools/check_samples.py            # verify all
    python3 tools/check_samples.py --list     # list case ids

Exit code 0 = every sample still reproduces its recorded value.
Exit code 1 = at least one drift was found (printed as DRIFT lines).

The manifest is a *characterization snapshot* of commit 809d7c1. A DRIFT line
means either the engine changed or the fixture was edited. That is the signal
that a documented boundary in docs/reinsurance-handover/ is now stale.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from reinsurance_bordereaux_normalizer.pipeline import run  # noqa: E402

MANIFEST = REPO / "samples" / "manifest.json"


def fail(msg: str) -> None:
    print(f"DRIFT {msg}")


def main() -> int:
    args = sys.argv[1:]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = manifest["cases"]

    if "--list" in args:
        for case in cases:
            print(f"{case['id']}: {case['title']}")
        return 0

    drift = 0
    for case in cases:
        payload = json.loads((REPO / "samples" / case["file"]).read_text(encoding="utf-8"))
        expected_error = case.get("expected_error")

        if expected_error:
            try:
                run(payload)
            except Exception as exc:  # noqa: BLE001 - we assert on the exact class
                actual_type = type(exc).__name__
                if actual_type != expected_error["type"] or expected_error["contains"] not in str(exc):
                    fail(
                        f"{case['id']}: expected {expected_error['type']} "
                        f"containing {expected_error['contains']!r}, "
                        f"got {actual_type}: {exc}"
                    )
                    drift += 1
                else:
                    print(f"PASS {case['id']} (raises {actual_type})")
            else:
                fail(f"{case['id']}: expected a {expected_error['type']} but the call succeeded")
                drift += 1
            continue

        actual = run(payload)
        expected = case["expected_result"]
        if actual != expected:
            fail(f"{case['id']}: result changed")
            for key in sorted(set(actual) | set(expected)):
                if actual.get(key) != expected.get(key):
                    print(f"       {key}: expected {expected.get(key)!r}")
                    print(f"       {key}: actual   {actual.get(key)!r}")
            drift += 1
        else:
            print(f"PASS {case['id']}")

    print()
    if drift:
        print(f"{drift} sample(s) drifted; review docs/reinsurance-handover/ boundaries.")
        return 1
    print(f"all {len(cases)} samples reproduce recorded values (rule_version={manifest['rule_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
