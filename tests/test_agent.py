from __future__ import annotations

import tempfile
import unittest

from salarygo.agent import AgentWorkflow, StrategyRepository, extract_funds, onboarding_questions, render_strategy_markdown
from tests.test_allocation import allocation_fixture


class AgentWorkflowTests(unittest.TestCase):
    def test_extracts_chinese_cny_and_sek_amounts(self) -> None:
        result = extract_funds("这个月有1.5万人民币可以投资，Revolut还有5000瑞典克朗")

        self.assertEqual(result["funds"], [{"amount": 15000.0, "currency": "CNY"}, {"amount": 5000.0, "currency": "SEK"}])
        self.assertEqual(result["missing"], [])

    def test_ambiguous_currency_causes_follow_up(self) -> None:
        result = extract_funds("这个月有15000可以投资")

        self.assertIn("currency", result["missing"])
        self.assertTrue(result["questions"])

    def test_onboarding_returns_only_missing_questions(self) -> None:
        questions = onboarding_questions({"investment_plan": {"horizon_years": 5}})

        self.assertFalse(any(item["field"] == "investment_plan.horizon_years" for item in questions))
        self.assertTrue(any(item["field"] == "accounts" for item in questions))

    def test_formal_strategy_is_saved_and_report_matches_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StrategyRepository(directory)
            result = AgentWorkflow(repository).monthly_request(
                "投入15000人民币和5000瑞典克朗", allocation_fixture()
            )
            record = repository.load(result["strategy"]["strategy_id"])
            report = render_strategy_markdown(record)

        self.assertEqual(result["status"], "formal")
        for item in record["plan"]["recommendations"]:
            self.assertIn(f"{item['amount']:,.2f}", report)

    def test_model_cannot_save_a_plan_that_failed_risk(self) -> None:
        context = allocation_fixture()
        context["candidates"][0]["data_fresh"] = False
        with tempfile.TemporaryDirectory() as directory:
            repository = StrategyRepository(directory)
            result = AgentWorkflow(repository).monthly_request("投入15000人民币", context)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(repository.list(), [])

    def test_execution_status_and_actual_trade_are_separate_from_advice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StrategyRepository(directory)
            result = AgentWorkflow(repository).monthly_request("投入15000人民币和5000瑞典克朗", allocation_fixture())
            strategy_id = result["strategy"]["strategy_id"]
            original = repository.load(strategy_id)["plan"]
            updated = repository.update_execution(strategy_id, "partially_executed", [{"asset_id": "bond", "amount": 1000}])

        self.assertEqual(updated["plan"], original)
        self.assertEqual(updated["execution_status"], "partially_executed")
        self.assertEqual(len(updated["actual_trades"]), 1)


if __name__ == "__main__":
    unittest.main()

