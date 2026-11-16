import json
import unittest
from pathlib import Path
from reinsurance_bordereaux_normalizer.pipeline import run
from reinsurance_bordereaux_normalizer.parser import load_records
from reinsurance_bordereaux_normalizer.engine import calculate

def fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "input.json").read_text())

class TestCore(unittest.TestCase):
    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)
        data = calculate(rows, payload)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], 'reinsurance-bordereaux-normalizer')

        assert result["allocations"][-1]["allocated"] == 50.0
