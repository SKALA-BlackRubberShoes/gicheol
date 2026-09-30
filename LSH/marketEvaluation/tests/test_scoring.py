from __future__ import annotations

import unittest

from LSH.marketEvaluation.schemas import CriterionResult
from LSH.marketEvaluation.scoring import calculate_market_score, scores_from_results


class MarketScoringTest(unittest.TestCase):
    def test_weighted_score_matches_documented_example(self):
        score_100, score_25 = calculate_market_score(
            {
                "customer_willingness": 4,
                "market_size": 3,
                "growth_timing": 4,
                "adoption_feasibility": 3,
                "scalability": 4,
            }
        )

        self.assertEqual(score_100, 71.0)
        self.assertEqual(score_25, 17.75)

    def test_missing_criterion_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_market_score(
                {
                    "customer_willingness": 4,
                    "market_size": 3,
                    "growth_timing": 4,
                    "adoption_feasibility": 3,
                }
            )

    def test_duplicate_criterion_is_rejected(self):
        repeated = [
            CriterionResult(
                criterion="customer_willingness",
                score=3,
                reason="test",
                source_ids=[],
            ),
            CriterionResult(
                criterion="customer_willingness",
                score=4,
                reason="duplicate",
                source_ids=[],
            ),
        ]

        with self.assertRaises(ValueError):
            scores_from_results(repeated)


if __name__ == "__main__":
    unittest.main()
