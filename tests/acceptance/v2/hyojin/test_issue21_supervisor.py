"""Actual Supervisor regression for failed target evidence (GitHub issue #21)."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


class EligibilityRoutingTests(unittest.TestCase):
    def run_scenario(self, scenario):
        path = Path(__file__).resolve().parents[3] / "fixtures/v2/hyojin/supervisor_regression.py"
        result = subprocess.run([sys.executable, str(path), scenario], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_failed_target_routes_to_eligibility_then_repaired_target_to_writer(self):
        result = self.run_scenario("recover")
        self.assertEqual(result["actions"], ["eligibility", "writer"])
        self.assertTrue(result["gate_pass"])
        self.assertEqual(set(result["research_attempts"].values()), {1})

    def test_still_unsupported_reply_rechecks_target_until_supported(self):
        result = self.run_scenario("repeat")
        self.assertEqual(result["actions"], ["eligibility", "eligibility", "writer"])
        self.assertTrue(result["gate_pass"])
        self.assertEqual(set(result["research_attempts"].values()), {1})

    def test_target_retry_limit_does_not_exhaust_research_attempts(self):
        result = self.run_scenario("exhausted")
        self.assertEqual(result["actions"], ["eligibility", "eligibility", "abort"])
        self.assertEqual(result["terminal_reason"], "ELIGIBILITY_UNKNOWN")
        self.assertFalse(result["gate_pass"])
        self.assertEqual(set(result["research_attempts"].values()), {1})


if __name__ == "__main__":
    unittest.main()
