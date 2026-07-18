from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from salarygo.application import SalaryGoApplication
from tests.test_allocation import allocation_fixture
from tests.test_ledger import example_ledger
from tests.test_profile import example_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EndToEndApplicationTests(unittest.TestCase):
    def test_complete_flow_is_resumable_and_never_auto_trades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SalaryGoApplication(directory)
            app.save_profile(example_profile(), expected_revision=0)
            app.save_ledger(example_ledger(), expected_revision=0)
            app.refresh([
                {"key": "cn-index", "kind": "fund", "symbol": "000300"},
                {"key": "hk-stock", "kind": "stock", "symbol": "HK-DEMO"},
                {"key": "us-etf", "kind": "fund", "symbol": "US-DEMO"},
                {"key": "USD_CNY", "kind": "fx", "symbol": "USD/CNY"},
            ], PROJECT_ROOT / "examples" / "market.fixture.json")
            context = allocation_fixture()
            app.save_allocation_context(context)
            before = app.ledger.load()
            result = app.generate_strategy("这个月投入15000人民币和5000瑞典克朗")
            after_advice = app.ledger.load()

            self.assertEqual(result["status"], "formal")
            self.assertEqual(before, after_advice, "生成建议不得自动修改持仓")

            strategy_id = result["strategy"]["strategy_id"]
            executed = app.record_execution(strategy_id, "partially_executed", [{
                "holding_id": "holding-cn", "type": "buy", "quantity": 10, "unit_price": 1.2,
                "amount": 12, "currency": "CNY", "fee": 0.1,
                "executed_at": "2026-07-18T20:30:00+08:00",
            }])
            resumed = SalaryGoApplication(directory).state()

        self.assertEqual(executed["ledger"]["holdings"][0]["quantity"], 1010)
        self.assertEqual(resumed["strategies"][0]["execution_status"], "partially_executed")
        self.assertEqual(resumed["ledger"]["holdings"][0]["quantity"], 1010)
        self.assertTrue(resumed["readiness"]["profile"])
        self.assertTrue(resumed["readiness"]["allocation_context"])

    def test_invalid_execution_does_not_mutate_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SalaryGoApplication(directory)
            app.save_ledger(example_ledger(), expected_revision=0)
            before = app.ledger.load()
            with self.assertRaises(FileNotFoundError):
                app.record_execution("missing", "executed", [])
            after = app.ledger.load()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

