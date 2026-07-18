from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from salarygo.backtest import render_backtest_markdown, run_backtest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def backtest_fixture() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "backtest.fixture.json").read_text(encoding="utf-8"))


class BacktestTests(unittest.TestCase):
    def test_fixed_input_is_reproducible(self) -> None:
        config = backtest_fixture()
        self.assertEqual(run_backtest(config), run_backtest(copy.deepcopy(config)))

    def test_gross_and_net_results_include_fees(self) -> None:
        report = run_backtest(backtest_fixture())
        for result in report["results"]:
            self.assertGreaterEqual(result["gross"]["ending_value"], result["net"]["ending_value"])
            self.assertEqual(result["gross"]["total_fees"], 0)
            self.assertGreater(result["net"]["total_fees"], 0)

    def test_dynamic_strategies_compare_to_fixed_dca(self) -> None:
        report = run_backtest(backtest_fixture())
        self.assertEqual(report["results"][0]["net"]["vs_fixed_dca"], 0)
        self.assertTrue(all("vs_fixed_dca" in result["net"] for result in report["results"]))

    def test_failure_period_and_drawdown_are_reported(self) -> None:
        report = run_backtest(backtest_fixture())
        for result in report["results"]:
            self.assertLess(result["net"]["worst_month"], 0)
            self.assertLess(result["net"]["maximum_drawdown"], 0)

    def test_future_signal_is_not_used(self) -> None:
        config = backtest_fixture()
        baseline = run_backtest(config)
        config["signals"].append({"published_at": "2027-01-01", "temperature": 0})
        scored = run_backtest(config)

        self.assertEqual(baseline, scored)
        self.assertTrue(all(
            audit["signal_published_at"] is None or audit["signal_published_at"] <= audit["period"]
            for result in scored["results"] for audit in result["net"]["signal_audit"]
        ))

    def test_report_contains_required_disclaimer(self) -> None:
        report = run_backtest(backtest_fixture())
        markdown = render_backtest_markdown(report)

        self.assertIn("历史模拟不代表未来收益", markdown)
        self.assertIn("最大回撤", markdown)
        self.assertIn("失败时期", markdown)


if __name__ == "__main__":
    unittest.main()

