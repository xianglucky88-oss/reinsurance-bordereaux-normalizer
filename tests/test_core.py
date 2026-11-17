import json
import unittest
from pathlib import Path

from reinsurance_bordereaux_normalizer.engine import calculate
from reinsurance_bordereaux_normalizer.parser import load_records, parse
from reinsurance_bordereaux_normalizer.pipeline import run

FIXTURE = Path(__file__).parents[1] / "fixtures" / "input.json"


def fixture():
    return json.loads(FIXTURE.read_text())


class TestCore(unittest.TestCase):
    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)

        parsed = parse(payload)
        data = calculate(parsed.movements, parsed.treaties)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)

        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], "reinsurance-bordereaux-normalizer")

    def test_fixture_allocation_is_exact(self):
        """C1 moves 120 then 80 against L1 [100,150) and L2 [150,250)."""
        result = run(fixture())
        rows = {
            (row["claim"], row["movement"], row["treaty"]): row["allocated_cents"]
            for row in result["allocations"]
        }
        self.assertEqual(
            rows,
            {
                ("C1", "M1", "L1"): 2000,
                ("C1", "M2", "L1"): 3000,
                ("C1", "M2", "L2"): 5000,
            },
        )
        self.assertEqual(result["claims"][0]["cumulative_cents"], 20000)
        self.assertEqual(result["claims"][0]["unallocated_cents"], 10000)

    def test_conservation_is_reported(self):
        result = run(fixture())
        self.assertTrue(result["audit"]["conservation"]["all_checks_pass"])
        self.assertEqual(result["audit"]["counts"]["input_rows"], 2)

    def test_replays_count_once(self):
        payload = fixture()
        payload["losses"].append(dict(payload["losses"][0]))
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["claims"][0]["cumulative_cents"], 20000)
        self.assertEqual(result["audit"]["counts"]["replayed_rows"], 1)

    def test_conflicting_replay_is_excluded(self):
        payload = fixture()
        conflicting = dict(payload["losses"][0])
        conflicting["amount"] = 121
        payload["losses"].append(conflicting)
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        # M1 is excluded entirely, leaving only M2 (80), which is below L1's
        # attachment of 100 and therefore allocates nothing.
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["claims"][0]["cumulative_cents"], 8000)
        self.assertEqual(result["audit"]["counts"]["conflict_keys"], 1)

    def test_negative_movement_is_an_adjustment(self):
        payload = {
            "losses": [
                {"claim": "C", "movement": "M1", "amount": 100, "currency": "USD"},
                {"claim": "C", "movement": "M2", "amount": -40, "currency": "USD"},
            ],
            "treaties": [{"name": "L1", "attachment": 0, "limit": 1000}],
        }
        result = run(payload)
        self.assertEqual(result["claims"][0]["cumulative_cents"], 6000)
        self.assertEqual(
            [row["allocated_cents"] for row in result["allocations"]], [10000, -4000]
        )

    def test_invalid_amount_is_rejected_not_zeroed(self):
        payload = {
            "losses": [{"claim": "C", "movement": "M", "amount": "abc", "currency": "USD"}],
            "treaties": [],
        }
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["audit"]["counts"]["rejected_rows"], 1)

    def test_input_order_is_not_significant(self):
        payload = fixture()
        reversed_payload = {**payload, "losses": list(reversed(payload["losses"]))}
        first = run(payload)
        second = run(reversed_payload)

        def conserved(result):
            return [
                (row["claim"], row["movement"], row["currency"], row["treaty"], row["allocated_cents"])
                for row in result["allocations"]
            ]

        # ``sources`` legitimately follow the real input positions, so only the
        # conserved quantities are order-independent.
        self.assertEqual(conserved(first), conserved(second))
        self.assertEqual(first["claims"], second["claims"])

    def test_overlapping_treaties_fail_closed(self):
        payload = {
            "losses": [{"claim": "C", "movement": "M", "amount": 100, "currency": "USD"}],
            "treaties": [
                {"name": "L1", "attachment": 0, "limit": 100},
                {"name": "L2", "attachment": 50, "limit": 100},
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["allocations"], [])
        self.assertFalse(result["audit"]["conservation"]["evaluated"])

    def test_empty_tower_allocates_nothing(self):
        payload = {"losses": [{"claim": "C", "movement": "M", "amount": 100, "currency": "USD"}]}
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["claims"][0]["unallocated_cents"], 10000)


if __name__ == "__main__":
    unittest.main()
