from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from salarygo.scoring import ScoreRepository, score_index_temperature, score_watchlist_stock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def scoring_fixture() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "scoring.fixture.json").read_text(encoding="utf-8"))


class ScoringTests(unittest.TestCase):
    def test_fixed_index_data_has_fixed_explainable_score(self) -> None:
        fixture = scoring_fixture()
        first = score_index_temperature(fixture["index"], as_of=fixture["as_of"])
        second = score_index_temperature(fixture["index"], as_of=fixture["as_of"])

        self.assertEqual(first["temperature"], second["temperature"])
        self.assertEqual(first["temperature"], 67.396656)
        self.assertEqual(first["scoring_version"], "index-temperature-v1")
        weighted = sum(
            first["components"][key]["score"] * first["weights"][key]
            for key in first["components"] if key in first["weights"]
        ) / sum(first["weights"][key] for key in first["components"] if key in first["weights"])
        self.assertAlmostEqual(first["temperature"], weighted, places=5)

    def test_future_index_data_does_not_change_historical_score(self) -> None:
        fixture = scoring_fixture()
        baseline = score_index_temperature(fixture["index"], as_of=fixture["as_of"])
        with_future = copy.deepcopy(fixture["index"])
        with_future["prices"].append({"as_of": "2027-01-01T00:00:00+08:00", "close": 999, "source": "future"})
        with_future["metrics"].append({"published_at": "2027-01-01T00:00:00+08:00", "valuation_percentile": 0.01, "volatility_annualized": 0.1, "portfolio_weight": 0.9, "target_weight": 0.6, "source": "future"})
        scored = score_index_temperature(with_future, as_of=fixture["as_of"])

        self.assertEqual(baseline["temperature"], scored["temperature"])
        self.assertEqual(scored["data_quality"]["future_records_excluded"], 2)

    def test_index_insufficient_or_stale_data_stops_eligibility(self) -> None:
        fixture = scoring_fixture()
        sparse = {"instrument_id": "sparse", "prices": fixture["index"]["prices"][-1:], "metrics": []}

        scored = score_index_temperature(sparse, as_of=fixture["as_of"])

        self.assertEqual(scored["confidence"], "stopped")
        self.assertFalse(scored["eligible"])

    def test_fixed_stock_data_has_components_and_high_confidence(self) -> None:
        fixture = scoring_fixture()
        scored = score_watchlist_stock(fixture["stock"], as_of=fixture["as_of"])

        self.assertEqual(scored["quality_score"], 79.013333)
        self.assertEqual(scored["scoring_version"], "watchlist-stock-v1")
        self.assertEqual(scored["confidence"], "high")
        self.assertTrue(scored["eligible"])
        self.assertEqual(len(scored["components"]), 10)
        self.assertTrue(scored["data_quality"]["source_complete"])

    def test_future_financial_report_is_not_used_at_historical_time(self) -> None:
        fixture = scoring_fixture()
        baseline = score_watchlist_stock(fixture["stock"], as_of=fixture["as_of"])
        future = copy.deepcopy(fixture["stock"])
        future["financials"].append({
            "period_end": "2026-12-31", "published_at": "2027-03-01T18:00:00+08:00",
            "revenue_growth": -0.9, "profit_growth": -0.9, "operating_cash_flow_positive": False,
            "free_cash_flow_positive": False, "roe": 0, "debt_ratio": 0.99, "interest_coverage": 0,
            "source": "future"
        })

        scored = score_watchlist_stock(future, as_of=fixture["as_of"])

        self.assertEqual(baseline["quality_score"], scored["quality_score"])
        self.assertEqual(scored["data_quality"]["future_records_excluded"], 1)

    def test_active_red_line_stops_stock_regardless_of_score(self) -> None:
        fixture = scoring_fixture()
        fixture["stock"]["red_flags"].append({
            "type": "adverse_audit", "active": True,
            "published_at": "2026-06-01T09:00:00+08:00", "detail": "示例审计红线", "source": "fixture-announcement"
        })

        scored = score_watchlist_stock(fixture["stock"], as_of=fixture["as_of"])

        self.assertFalse(scored["eligible"])
        self.assertEqual(scored["confidence"], "stopped")
        self.assertEqual(scored["red_lines"][0]["type"], "adverse_audit")

    def test_missing_stock_metrics_reduce_confidence_or_stop(self) -> None:
        fixture = scoring_fixture()
        for key in ("profit_growth", "free_cash_flow_positive", "roe", "interest_coverage"):
            fixture["stock"]["financials"][0].pop(key)

        scored = score_watchlist_stock(fixture["stock"], as_of=fixture["as_of"])

        self.assertIn(scored["confidence"], {"low", "stopped"})
        self.assertNotEqual(scored["confidence"], "high")

    def test_score_records_preserve_scoring_version_history(self) -> None:
        fixture = scoring_fixture()
        score = score_index_temperature(fixture["index"], as_of=fixture["as_of"])
        with tempfile.TemporaryDirectory() as directory:
            repository = ScoreRepository(directory)
            first = repository.record(score, expected_revision=0)
            second = repository.record(score, expected_revision=1)

        self.assertEqual(second["revision"], 2)
        self.assertEqual([item["scoring_version"] for item in second["records"]], ["index-temperature-v1"] * 2)
        self.assertEqual(first["records"][0]["as_of"], fixture["as_of"])


if __name__ == "__main__":
    unittest.main()
