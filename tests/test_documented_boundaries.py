"""Regression tests for the boundaries documented in docs/reinsurance-handover/.

Each test is named after the boundary it pins (P1..P17 in 01-rule-baseline.md).
These are *characterization* tests: they assert current behaviour, including the
behaviour that is wrong on purpose-but-known. If one of these fails, the engine
changed and the handover documents must be updated alongside.
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from reinsurance_bordereaux_normalizer.pipeline import run  # noqa: E402

SAMPLES = REPO / "samples"


def load(case_id):
    return json.loads((SAMPLES / f"{case_id}.json").read_text(encoding="utf-8"))


def alloc(result, treaty=None):
    rows = result["allocations"]
    if treaty is not None:
        rows = [r for r in rows if r["treaty"] == treaty]
    return rows


class TestDocumentedBoundaries(unittest.TestCase):
    def test_P1_cumulative_is_global_across_claims(self):
        """Two unrelated claims share one running cumulative (F02)."""
        result = run(load("F02-two-claims-global-pool"))
        self.assertEqual(result["cumulative_loss"], 240.0)
        # C2 only contributes 30 because C1 already pushed past the attachment.
        c2 = [r for r in result["allocations"] if r["claim"] == "C2"]
        self.assertEqual(c2[0]["allocated"], 30.0)

    def test_P2_file_order_changes_allocation(self):
        """Same movements, different row order, different attribution (F03a/F03b)."""
        forward = run(load("F03a-movement-order-forward"))
        reversed_ = run(load("F03b-movement-order-reversed"))
        self.assertEqual(forward["cumulative_loss"], reversed_["cumulative_loss"])
        self.assertNotEqual(forward["allocations"], reversed_["allocations"])
        self.assertEqual(len(forward["allocations"]), 2)
        self.assertEqual(len(reversed_["allocations"]), 1)

    def test_P3_overlapping_layers_double_count(self):
        """Overlap is not netted (F04)."""
        result = run(load("F04-overlapping-layers-double-count"))
        self.assertEqual(len(alloc(result, "L1")), 1)
        self.assertEqual(len(alloc(result, "L2")), 1)
        # 50 of loss sits in both layers.
        self.assertEqual(alloc(result, "L1")[0]["allocated"], 100.0)
        self.assertEqual(alloc(result, "L2")[0]["allocated"], 100.0)

    def test_P4_gap_is_indistinguishable_from_above_top(self):
        """Gap loss lands in unallocated next to above-top loss (F05)."""
        result = run(load("F05-layer-gap-unallocated"))
        self.assertEqual(result["unallocated"], 300.0)

    def test_P5_unallocated_is_floored_so_overallocation_is_hidden(self):
        """Allocations exceed cumulative but unallocated reports 0.0 (F06)."""
        result = run(load("F06-identical-layers-masked-unallocated"))
        total = sum(r["allocated"] for r in result["allocations"])
        self.assertEqual(total, 200.0)
        self.assertEqual(result["cumulative_loss"], 100.0)
        self.assertEqual(result["unallocated"], 0.0)  # not -100.0

    def test_P6_currency_is_never_read(self):
        """USD and EUR are summed as bare numbers (F07)."""
        result = run(load("F07-mixed-currency-no-fx"))
        self.assertEqual(result["cumulative_loss"], 200.0)
        self.assertNotIn("currency", json.dumps(result["allocations"]))

    def test_P7_rounding_is_half_to_even(self):
        """0.125 -> 0.12 and 0.135 -> 0.14, not half-up (F08a/F08b)."""
        low = run(load("F08a-rounding-half-even-low"))
        high = run(load("F08b-rounding-half-even-high"))
        self.assertEqual(alloc(low)[0]["allocated"], 0.12)
        self.assertEqual(alloc(high)[0]["allocated"], 0.14)

    def test_P8_subcent_movements_vanish_from_detail(self):
        """0.004 + 0.004 emits no allocation rows (F09)."""
        result = run(load("F09-subcent-vanish"))
        self.assertEqual(result["allocations"], [])
        self.assertAlmostEqual(result["cumulative_loss"], 0.008)

    def test_P9_duplicate_rows_are_counted_twice(self):
        """No dedup on claim+movement (F10)."""
        result = run(load("F10-duplicate-row"))
        self.assertEqual(result["cumulative_loss"], 240.0)
        self.assertEqual(len(result["allocations"]), 2)
        self.assertEqual({r["movement"] for r in result["allocations"]}, {"M1"})

    def test_P10_full_history_and_delta_disagree(self):
        """Same movement allocates 100 in full-history and 0 in delta-only (F11a/F11b)."""
        full = run(load("F11a-full-history"))
        delta = run(load("F11b-month-only-delta"))
        self.assertEqual(alloc(full, "L1")[0]["allocated"], 100.0)
        self.assertEqual(delta["allocations"], [])

    def test_P11_recovery_frees_capacity_without_premium(self):
        """A recovery lowers cumulative and re-uses layer capacity (F12)."""
        result = run(load("F12-recovery-reduces-cumulative"))
        by_movement = {r["movement"]: r["allocated"] for r in result["allocations"]}
        self.assertEqual(by_movement, {"M1": 100.0, "M2": -50.0, "M3": 50.0})
        self.assertEqual(sum(by_movement.values()), 100.0)

    def test_P12_numeric_string_is_coerced_silently(self):
        """'120' becomes 120.0 with no warning (F13)."""
        result = run(load("F13-string-amount-coerced"))
        self.assertEqual(result["cumulative_loss"], 120.0)

    def test_P13_attachment_ties_keep_input_order(self):
        """Stable sort means B stays ahead of A (F14)."""
        result = run(load("F14-attachment-tie-input-order"))
        self.assertEqual([r["treaty"] for r in result["allocations"]], ["B", "A"])

    def test_P14_treaty_change_applies_to_whole_run(self):
        """v2 limit reprices M2 from 30 to 80 (F15)."""
        result = run(load("F15-treaty-limit-change-v2"))
        m2_l1 = [r for r in result["allocations"] if r["movement"] == "M2" and r["treaty"] == "L1"]
        self.assertEqual(m2_l1[0]["allocated"], 80.0)

    def test_P15_zero_amount_leaves_no_trace(self):
        """A zero movement emits no row (F16)."""
        result = run(load("F16-zero-amount-suppressed"))
        self.assertNotIn("M0", {r["movement"] for r in result["allocations"]})

    def test_P16_per_movement_rounding_does_not_telescope(self):
        """Sum of allocations != cumulative for 200 x 0.005 (F17)."""
        result = run(load("F17-rounding-drift-200-movements"))
        total = sum(r["allocated"] for r in result["allocations"])
        self.assertGreater(total - result["cumulative_loss"], 0.8)

    def test_P17_non_object_rows_are_dropped_without_a_counter(self):
        """3 malformed rows vanish silently (F18)."""
        result = run(load("F18-silent-row-drop"))
        self.assertEqual(result["cumulative_loss"], 120.0)
        self.assertEqual(len(result["allocations"]), 1)


class TestVersioningSamples(unittest.TestCase):
    def test_F19_resubmission_doubles_the_run(self):
        once = run(load("F01-cross-layer-trace"))
        twice = run(load("F19-resubmitted-file-concatenation"))
        self.assertEqual(twice["cumulative_loss"], once["cumulative_loss"] * 2)
        self.assertEqual(twice["status"], "ok")

    def test_F20_narrowing_a_layer_restates_a_prior_movement(self):
        result = run(load("F20-retroactive-restatement-narrowing"))
        self.assertEqual(alloc(result)[0]["allocated"], 20.0)

    def test_F21_is_the_v1_rollback_baseline(self):
        self.assertEqual(run(load("F21-rollback-baseline-v1")), run(load("F01-cross-layer-trace")))


class TestErrorContract(unittest.TestCase):
    def _assert_raises(self, case_id, exc_type, fragment):
        with self.assertRaises(exc_type) as ctx:
            run(load(case_id))
        self.assertIn(fragment, str(ctx.exception))

    def test_E01_missing_amount(self):
        self._assert_raises("E01-missing-amount", KeyError, "amount")

    def test_E02_string_attachment_fails_the_whole_run(self):
        self._assert_raises("E02-attachment-as-string", TypeError, "<")

    def test_E03_losses_must_be_a_list(self):
        self._assert_raises("E03-losses-not-list", ValueError, "losses must be a list")

    def test_E04_non_numeric_amount(self):
        self._assert_raises("E04-amount-not-numeric", ValueError, "could not convert")


if __name__ == "__main__":
    unittest.main()
