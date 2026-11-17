#!/usr/bin/env python3
"""Trace every layer allocation back to its input rows and rule lines.

Usage:
    python3 tools/trace_allocation.py samples/F01-cross-layer-trace.json
    python3 tools/trace_allocation.py samples/F01-cross-layer-trace.json --claim C1 --treaty L2

This re-derives the engine's arithmetic independently and prints, per emitted
allocation row, the before/after cumulative bounds, the layer band, the exact
formula, and the source line that implements it. It is a read-only explainer:
it does not import the engine's calculate(), only its documented formula, so a
disagreement between this trace and the CLI is itself a bug signal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from reinsurance_bordereaux_normalizer.pipeline import run  # noqa: E402

RULE_VERSION = "R-809d7c1.1"

# (source file, line, quoted code) for each rule the allocation depends on.
RULE_LINES = {
    "sort": ("src/reinsurance_bordereaux_normalizer/engine.py", 2,
             'sorted(payload.get("treaties", []), key=lambda t: t["attachment"])'),
    "accumulate": ("src/reinsurance_bordereaux_normalizer/engine.py", 4,
                   'start = cumulative; cumulative += float(loss["amount"])'),
    "band_low": ("src/reinsurance_bordereaux_normalizer/engine.py", 6,
                 'covered_before = max(0.0, min(start, treaty["attachment"] + treaty["limit"]) - treaty["attachment"])'),
    "band_high": ("src/reinsurance_bordereaux_normalizer/engine.py", 7,
                  'covered_after = max(0.0, min(cumulative, treaty["attachment"] + treaty["limit"]) - treaty["attachment"])'),
    "round": ("src/reinsurance_bordereaux_normalizer/engine.py", 8,
              'amount = round(covered_after - covered_before, 2)'),
    "emit": ("src/reinsurance_bordereaux_normalizer/engine.py", 9,
             'if amount: allocations.append({...})'),
    "unallocated": ("src/reinsurance_bordereaux_normalizer/engine.py", 10,
                    '"unallocated": max(0.0, cumulative - sum(a["allocated"] for a in allocations))'),
    "parse": ("src/reinsurance_bordereaux_normalizer/parser.py", 2,
              'rows = payload.get("losses", [])'),
}

COLUMN_MAP = {
    "claim": "losses[].claim",
    "movement": "losses[].movement",
    "amount": "losses[].amount",
    "currency": "losses[].currency (READ? no - never referenced by engine.py)",
    "attachment": "treaties[].attachment",
    "limit": "treaties[].limit",
    "name": "treaties[].name",
}


def cite(rule: str) -> str:
    path, line, code = RULE_LINES[rule]
    return f"{path}:{line}  {code}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--claim", help="only trace this claim")
    ap.add_argument("--treaty", help="only trace this treaty name")
    args = ap.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    losses = [r for r in payload.get("losses", []) if isinstance(r, dict)]
    treaties = sorted(payload.get("treaties", []), key=lambda t: t["attachment"])
    result = run(payload)

    print(f"rule_version : {RULE_VERSION}")
    print(f"input        : {args.input}")
    print(f"source columns (only these are read):")
    for field, origin in COLUMN_MAP.items():
        print(f"  {field:<10} <- {origin}")
    print()
    print(f"treaty order after sort by attachment ({cite('sort')}):")
    for idx, t in enumerate(treaties):
        print(f"  [{idx}] {t['name']!r} attachment={t['attachment']} limit={t['limit']} "
              f"band=[{t['attachment']}, {t['attachment'] + t['limit']}]")
    print()

    cumulative = 0.0
    emitted: list[dict] = []
    for row_no, loss in enumerate(losses):
        start = cumulative
        cumulative += float(loss["amount"])
        if args.claim and loss["claim"] != args.claim:
            continue
        print(f"ROW {row_no} claim={loss['claim']!r} movement={loss['movement']!r} "
              f"amount={loss['amount']!r}  ({cite('accumulate')})")
        print(f"  cumulative: {start} -> {cumulative}")
        for t in treaties:
            if args.treaty and t["name"] != args.treaty:
                continue
            top = t["attachment"] + t["limit"]
            before = max(0.0, min(start, top) - t["attachment"])
            after = max(0.0, min(cumulative, top) - t["attachment"])
            amount = round(after - before, 2)
            if amount:
                emitted.append({"claim": loss["claim"], "movement": loss["movement"],
                                "treaty": t["name"], "allocated": amount})
                print(f"  -> {t['name']!r}: band=[{t['attachment']}, {top}] "
                      f"covered_before={before} covered_after={after} "
                      f"delta={after - before} round2={amount}  EMITTED")
            else:
                print(f"  .. {t['name']!r}: band=[{t['attachment']}, {top}] "
                      f"covered_before={before} covered_after={after} "
                      f"delta={after - before} round2={amount}  (suppressed by line 9)")
        print()

    print("reported unallocated:", result["unallocated"], f"({cite('unallocated')})")
    print("  NOTE: floored at 0, so over-allocation from overlapping layers is invisible.")

    # Cross-check the independent recomputation against the real engine.
    filtered = [
        r for r in result["allocations"]
        if (not args.claim or r["claim"] == args.claim) and (not args.treaty or r["treaty"] == args.treaty)
    ]
    if filtered != emitted:
        print("\nMISMATCH: trace disagrees with engine output; treat as a defect.")
        return 1
    print(f"\ntrace matches engine output ({len(emitted)} allocation rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
