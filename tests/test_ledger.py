from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from salarygo.ledger import LedgerRepository, LedgerService, value_portfolio

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def example_ledger() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "ledger.example.json").read_text(encoding="utf-8"))


class LedgerTests(unittest.TestCase):
    def test_fixed_portfolio_values_and_shares_are_correct(self) -> None:
        result = value_portfolio(example_ledger(), {"HKD": 0.9, "USD": 7.2, "SEK": 0.68})

        self.assertEqual(result["known_market_value"], 2180.0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_holding_ids"], ["holding-us"])
        buckets = {item["key"]: item for item in result["concentration"]["bucket"]}
        self.assertAlmostEqual(buckets["index_core"]["known_value_share"], 1100 / 2180)
        self.assertAlmostEqual(buckets["individual_stock"]["known_value_share"], 1080 / 2180)

    def test_missing_price_never_becomes_zero_market_value(self) -> None:
        result = value_portfolio(example_ledger(), {"HKD": 0.9, "USD": 7.2})
        us = next(item for item in result["holdings"] if item["holding_id"] == "holding-us")

        self.assertIsNone(us["market_value"])
        self.assertEqual(us["missing"], ["price"])

    def test_missing_fx_is_reported(self) -> None:
        result = value_portfolio(example_ledger(), {})
        hk = next(item for item in result["holdings"] if item["holding_id"] == "holding-hk")

        self.assertIsNone(hk["market_value"])
        self.assertIn("fx:HKD->CNY", hk["missing"])

    def test_eastmoney_hk_connect_and_revolut_are_separately_aggregated(self) -> None:
        ledger = example_ledger()
        ledger["holdings"][2]["current_price"] = 100
        ledger["holdings"][2]["price_as_of"] = "2026-07-18T08:00:00+08:00"
        result = value_portfolio(ledger, {"HKD": 0.9, "USD": 7.2})
        accounts = {item["key"]: item["market_value"] for item in result["concentration"]["account"]}

        self.assertEqual(accounts, {"eastmoney-a": 1100.0, "eastmoney-hk": 1080.0, "revolut": 7200.0})

    def test_manual_add_edit_delete_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = LedgerRepository(directory)
            saved = repository.save(example_ledger(), expected_revision=0)
            service = LedgerService(repository)
            added = service.add("transaction", {
                "id": "trade-1", "account_id": "eastmoney-a", "instrument_id": "cn-index",
                "type": "buy", "quantity": 1000, "unit_price": 1.0,
                "amount": 1000, "currency": "CNY", "fee": 1,
                "executed_at": "2026-07-18T08:00:00+08:00", "notes": "手动成交"
            })
            self.assertEqual(added["revision"], saved["revision"] + 1)
            service.update("holding", "holding-cn", {"quantity": 1200})
            service.delete("transaction", "trade-1")
            reloaded = repository.load()

            self.assertEqual(reloaded["holdings"][0]["quantity"], 1200)
            self.assertEqual(reloaded["transactions"], [])


if __name__ == "__main__":
    unittest.main()
