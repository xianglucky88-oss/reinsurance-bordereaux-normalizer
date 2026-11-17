#!/usr/bin/env python3
"""Validate the handover registers against the actual source tree.

Checks performed
----------------
1. Every `read_at` entry of the form `path:line` must exist and the line must
   still look like the field access it claims to be (the field name appears).
2. Every `NOT_IMPLEMENTED` field's absence claim must still hold: a word-boundary
   grep over src/ returns zero matches for at least one of its keywords.
3. Every `sample` / `impact_sample` referenced by a register must exist in
   samples/manifest.json.
4. The rule_version in both registers must match the manifest.

Exit 0 = registers are consistent with the code. Exit 1 = a cited line moved,
a gap was silently implemented, or a sample reference is dangling.

Usage:
    python3 tools/check_registers.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
MANIFEST = REPO / "samples" / "manifest.json"
REGISTERS = [
    REPO / "registers" / "bordereaux_field_lineage.json",
    REPO / "registers" / "treaty_change_register.json",
]

problems: list[str] = []


def note(msg: str) -> None:
    problems.append(msg)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_lines(path: str) -> list[str]:
    return (REPO / path).read_text(encoding="utf-8").splitlines()


def check_read_at(column: dict) -> None:
    field = column["normalized_field"].split(".")[-1].rstrip("[]")
    if field.startswith("derived") or field.startswith("cumulative") or field == "unallocated":
        return
    for ref in column.get("read_at", []):
        path, _, lineno = ref.rpartition(":")
        if not path or not lineno.isdigit():
            note(f"{column['id']}: malformed read_at {ref!r}")
            continue
        if not (REPO / path).exists():
            note(f"{column['id']}: read_at path missing: {path}")
            continue
        lines = source_lines(path)
        n = int(lineno)
        if n > len(lines):
            note(f"{column['id']}: read_at {ref} is past end of file ({len(lines)} lines)")
            continue
        # The cited line should mention the field or a documented alias of it.
        aliases = {field}
        if field == "limit":
            aliases.add("limit")
        line = lines[n - 1]
        if not any(a and a in line for a in aliases):
            note(
                f"{column['id']}: read_at {ref} no longer mentions {field!r}; got: {line.strip()!r}"
            )


def absence_count(keywords: str) -> int:
    """Count word-boundary matches of any keyword across src/*.py."""
    total = 0
    pattern = re.compile(rf"\b(?:{keywords})\b", re.IGNORECASE)
    for py in SRC.rglob("*.py"):
        total += len(pattern.findall(py.read_text(encoding="utf-8")))
    return total


def check_absence(item: dict) -> None:
    # The register stores prose in `evidence`; re-derive the keyword set from
    # the id so the check is self-contained rather than parsing prose.
    expected_zero = {
        "NI-FX": r"fx|rate|exchange",
        "NI-TREATY-VERSION": r"version|effective",
        "NI-LOSS-DATE": r"date|month|period",
        "NI-FILE-IDENTITY": r"file_id|hash|batch|dedup",
        "NI-CEDED-SHARE": r"ceded|share|percent|participation",
        "NI-REINSTATEMENT": r"reinstat|premium",
        "NI-CORRECTION-LINK": r"correction|supersede|reversal",
    }
    key = item["id"]
    if key not in expected_zero:
        note(f"{key}: no absence check defined; add one or this gap is unverified")
        return
    count = absence_count(expected_zero[key])
    if count != 0:
        note(
            f"{key}: expected 0 matches for /{expected_zero[key]}/ in src/ "
            f"but found {count} - this gap may now be implemented; update the register and docs"
        )


def normalize_sample_ref(ref: str) -> str:
    """Turn 'samples/F01-x.json' into 'F01-x' so it matches manifest ids."""
    ref = ref.strip()
    if ref.startswith("samples/"):
        ref = ref[len("samples/"):]
    if ref.endswith(".json"):
        ref = ref[: -len(".json")]
    return ref


def collect_sample_refs(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"sample", "impact_sample", "source_sample", "evidence_sample"} and isinstance(v, str):
                for part in v.split(","):
                    out.add(normalize_sample_ref(part))
            elif k == "evidence_samples" and isinstance(v, list):
                out.update(normalize_sample_ref(str(x)) for x in v)
            else:
                collect_sample_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_sample_refs(v, out)


def main() -> int:
    manifest = load(MANIFEST)
    known = {c["id"] for c in manifest["cases"]}
    rule_version = manifest["rule_version"]

    for reg_path in REGISTERS:
        reg = load(reg_path)
        rel = reg_path.relative_to(REPO)
        if reg.get("rule_version") != rule_version:
            note(f"{rel}: rule_version {reg.get('rule_version')!r} != manifest {rule_version!r}")

        for column in reg.get("columns", []):
            check_read_at(column)
        for item in reg.get("not_implemented_business_columns", []):
            check_absence(item)

        refs: set[str] = set()
        collect_sample_refs(reg, refs)
        for ref in sorted(refs):
            if ref and ref not in known:
                note(f"{rel}: references unknown sample {ref!r}")

        if "versions" in reg:
            for version in reg["versions"]:
                if version.get("effective_from") not in (None, "not-representable"):
                    note(
                        f"{rel}: {version['treaty_id']}/{version['version']} has a concrete "
                        f"effective_from but the engine has no effective-date support"
                    )

    if problems:
        print(f"{len(problems)} register problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"registers consistent with source (rule_version={rule_version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
