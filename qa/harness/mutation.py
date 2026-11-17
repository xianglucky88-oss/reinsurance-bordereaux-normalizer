"""Mutation testing for the settlement semantics.

Each mutant is a small, meaningful edit to the production source: swapping a
comparison, changing a rounding mode, dropping a dedupe, ignoring currency,
treating a negative movement as positive, and so on.  A mutant is *killed* when
the QA harness reports a failure against the mutated copy; a mutant that
*survives* means the suite does not actually test that semantic.

The runner copies ``src/`` into a scratch directory, applies one textual edit,
and re-runs a fast but representative subset of the suite in a subprocess with
``RBN_SRC`` pointing at the mutated copy.  The working tree is never modified.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
QA_ROOT = HERE.parent
REPO_ROOT = QA_ROOT.parent
SRC = REPO_ROOT / "src"

# (id, relative file, old text, new text, description)
MUTANTS: List[Tuple[str, str, str, str, str]] = [
    (
        "round-half-even",
        "reinsurance_bordereaux_normalizer/parser.py",
        "ROUND_HALF_UP",
        "ROUND_HALF_EVEN",
        "Rounding mode changed from half-up to half-even.",
    ),
    (
        "round-truncate",
        "reinsurance_bordereaux_normalizer/parser.py",
        "AMOUNT_QUANTUM, rounding=ROUND_HALF_UP",
        "AMOUNT_QUANTUM, rounding=ROUND_DOWN",
        "Amounff normalization truncates instead of rounding.",
    ),
    (
        "negative-as-positive",
        "reinsurance_bordereaux_normalizer/engine.py",
        "after = before + to_cents(movement.amount)",
        "after = before + abs(to_cents(movement.amount))",
        "Negative movements treated as fresh positive loss.",
    ),
    (
        "no-dedupe",
        "reinsurance_bordereaux_normalizer/engine.py",
        "        if previous is None:",
        "        if True:",
        "Replays are applied more than once.",
    ),
    (
        "ignore-currency-scope",
        "reinsurance_bordereaux_normalizer/engine.py",
        "applicable = [t for t in treaties if t.currency is None or t.currency == currency]",
        "applicable = list(treaties)",
        "Currency-scoped layers absorb other currencies.",
    ),
    (
        "swap-attachment-clamp",
        "reinsurance_bordereaux_normalizer/engine.py",
        "return max(0, min(position_cents, attachment + limit) - attachment)",
        "return max(0, min(position_cents, attachment + limit) - limit)",
        "Layer coverage measured from the limit instead of the attachment.",
    ),
    (
        "drop-limit-ceiling",
        "reinsurance_bordereaux_normalizer/engine.py",
        "return max(0, min(position_cents, attachment + limit) - attachment)",
        "return max(0, position_cents - attachment)",
        "Coverage is not capped at the layer limit (loss above the tower is allocated).",
    ),
    (
        "allocation-not-incremental",
        "reinsurance_bordereaux_normalizer/engine.py",
        "allocated = _coverage_cents(after, treaty) - _coverage_cents(before, treaty)",
        "allocated = _coverage_cents(after, treaty)",
        "Allocation uses absolute coverage, double-counting earlier movements.",
    ),
    (
        "first-wins-conflict",
        "reinsurance_bordereaux_normalizer/parser.py",
        "        if len(distinct) > 1:",
        "        if len(distinct) > 99:",
        "Conflicting replays are silently accepted (first value wins).",
    ),
    (
        "negative-zero",
        "reinsurance_bordereaux_normalizer/parser.py",
        '    sign = "-" if cents < 0 else ""',
        '    sign = "-" if cents <= 0 else ""',
        "Zero amounts render as the negative zero ``-0.00``.",
    ),
    (
        "skip-significance-check",
        "reinsurance_bordereaux_normalizer/parser.py",
        "    try:\n        rounded = decimal_value.quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)\n    except InvalidOperation:",
        "    try:\n        rounded = Decimal(int(decimal_value))\n    except InvalidOperation:",
        "Sub-cent values are truncated to whole units instead of rounded to cents.",
    ),
    (
        "skip-currency-validation",
        "reinsurance_bordereaux_normalizer/parser.py",
        "    if not _CURRENCY_RE.match(text):\n        raise FieldError(\"invalid_currency\", \"currency must be a three-letter code\")\n    return text",
        "    return text",
        "Invalid currency codes are accepted.",
    ),
    (
        "skip-range-check",
        "reinsurance_bordereaux_normalizer/parser.py",
        "    if abs(decimal_value) > MAX_ABS_AMOUNT:",
        "    if False:",
        "Out-of-range amounts are accepted.",
    ),
    (
        "overlap-allowed",
        "reinsurance_bordereaux_normalizer/parser.py",
        "            if current.attachment < previous_top:",
        "            if current.attachment < previous_top - 1000000:",
        "Overlapping treaty layers are accepted.",
    ),
    (
        "no-canonical-ordering",
        "reinsurance_bordereaux_normalizer/engine.py",
        "    return sorted(\n        movements,\n        key=lambda m: (m.claim, m.currency, m.movement, m.source_indices),\n    )",
        "    return list(movements)",
        "Movements are applied in input order, breaking permutation invariance.",
    ),
    (
        "conserve-per-treaty-not-claim",
        "reinsurance_bordereaux_normalizer/report.py",
        "                if row[\"treaty\"] == treaty.name\n                and row[\"currency\"] == currency\n                and row[\"claim\"] == claim",
        "                if row[\"treaty\"] == treaty.name\n                and row[\"currency\"] == currency",
        "Layer limit is checked across claims instead of per claim.",
    ),
    (
        "elastic-status",
        "reinsurance_bordereaux_normalizer/report.py",
        "        if errors:\n            return \"partial\"",
        "        if False:\n            return \"partial\"",
        "Row-level errors do not downgrade status to partial.",
    ),
    (
        "trace-drops-movement",
        "reinsurance_bordereaux_normalizer/report.py",
        "                \"movement\": row[\"movement\"],\n                \"currency\": row[\"currency\"],\n                \"treaty\": row[\"treaty\"],",
        "                \"movement\": None,\n                \"currency\": row[\"currency\"],\n                \"treaty\": row[\"treaty\"],",
        "Audit trace loses movement identity.",
    ),
    (
        "fatal-not-fatal",
        "reinsurance_bordereaux_normalizer/pipeline.py",
        "    if parsed.fatal_errors:",
        "    if False:",
        "Fatal configuration errors are processed anyway.",
    ),
    (
        "swallow-faults",
        "reinsurance_bordereaux_normalizer/pipeline.py",
        "        if fault == stage:\n            raise _Fault(stage)",
        "        if fault == stage:\n            return None",
        "Injected faults are swallowed instead of failing closed.",
    ),
    (
        "currency-blind-layer-check",
        "reinsurance_bordereaux_normalizer/engine.py",
        "    applicable = [t for t in treaties if t.currency is None or t.currency == currency]\n    return sorted(applicable",
        "    applicable = [t for t in treaties if t.currency is not None]\n    return sorted(applicable",
        "Layers only apply to their own currency and ignore unscoped movements.",
    ),
]

# A representative subset that exercises every mutant class and runs quickly.
MUTATION_SCRIPT = textwrap.dedent(
    '''
    import sys
    from harness.core import Harness
    from harness.layers import LAYER_CHECKS
    from harness.scenarios import SCENARIOS
    from harness.faults import (
        cli_exit_codes, inject_faults, sweep_attachment_and_top, sweep_rounding,
    )

    harness = Harness()
    for scenario in SCENARIOS:
        scenario(harness)
    for layer_check in LAYER_CHECKS:
        layer_check(harness)
    sweep_attachment_and_top(harness)
    sweep_rounding(harness)
    cli_exit_codes(harness)
    inject_faults(harness)
    summary = harness.summary()
    print("FAILURES=" + str(summary["failed"]))
    for failure in summary["failures"][:3]:
        print("  " + failure["name"] + " :: " + failure["detail"][:120])
    # Exit 0 means the suite passed against the mutant (mutant survived);
    # exit 1 means a check failed (mutant killed).
    raise SystemExit(1 if summary["failed"] else 0)
    '''
)


@dataclass
class MutantOutcome:
    mutant_id: str
    description: str
    status: str  # "killed", "survived", or "not-applied"
    detail: str = ""


def _prepare_scratch(root: Path) -> Path:
    target = root / "src"
    shutil.copytree(SRC, target)
    return target


def _apply_mutation(scratch_src: Path, relative: Path, old: str, new: str) -> bool:
    path = scratch_src / relative
    text = path.read_text()
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1))
    return True


def run_mutant(
    mutant_id: str,
    relative: str,
    old: str,
    new: str,
    description: str,
    timeout: int = 180,
) -> MutantOutcome:
    with tempfile.TemporaryDirectory(prefix="rbn-mutant-") as tmp:
        root = Path(tmp)
        scratch_src = _prepare_scratch(root)
        if not _apply_mutation(scratch_src, Path(relative), old, new):
            return MutantOutcome(mutant_id, description, "not-applied", "source text not found")

        env = {
            "PYTHONPATH": f"{scratch_src}:{QA_ROOT}",
            "PATH": "/usr/bin:/bin",
            "RBN_SRC": str(scratch_src),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, "-c", MUTATION_SCRIPT],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=timeout,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode == 0:
            return MutantOutcome(mutant_id, description, "survived", output[-400:])
        detail = "\n".join(line for line in output.splitlines() if line.strip())[:400]
        return MutantOutcome(mutant_id, description, "killed", detail)


def run_mutation_suite(harness: "object") -> List[MutantOutcome]:
    """Run every mutant, recording kill/survive as harness checks."""
    outcomes: List[MutantOutcome] = []
    for mutant_id, relative, old, new, description in MUTANTS:
        outcome = run_mutant(mutant_id, relative, old, new, description)
        outcomes.append(outcome)
        harness.check(
            f"mutation:{mutant_id}",
            outcome.status == "killed",
            f"{outcome.status}: {outcome.detail}",
        )
    return outcomes


def summary(outcomes: Sequence[MutantOutcome]) -> dict:
    killed = sum(1 for outcome in outcomes if outcome.status == "killed")
    survived = [outcome.mutant_id for outcome in outcomes if outcome.status == "survived"]
    not_applied = [outcome.mutant_id for outcome in outcomes if outcome.status == "not-applied"]
    return {
        "total": len(outcomes),
        "killed": killed,
        "survived": survived,
        "not_applied": not_applied,
        "mutation_score": round(killed / len(outcomes), 4) if outcomes else 0.0,
    }
