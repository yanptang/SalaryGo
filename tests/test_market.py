from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from salarygo.market import (
    MarketCacheRepository,
    MarketRefreshService,
    MarketRequest,
    StaticMarketDataProvider,
    apply_quotes_to_ledger,
)
from tests.test_ledger import example_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MarketDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = MarketCacheRepository(self.temporary.name)
        self.service = MarketRefreshService(self.repository)
        self.provider = StaticMarketDataProvider.from_file(PROJECT_ROOT / "examples" / "market.fixture.json")

    def test_stock_fund_index_nav_and_fx_interface_can_refresh(self) -> None:
        requests = [
            MarketRequest("cn-index", "index", "000300"),
            MarketRequest("hk-stock", "stock", "HK-DEMO"),
            MarketRequest("us-etf", "fund", "US-DEMO"),
            MarketRequest("cn-fund-nav", "nav", "CN-NAV-DEMO"),
            MarketRequest("USD_CNY", "fx", "USD/CNY"),
        ]
        result = self.service.refresh(requests, self.provider)

        self.assertTrue(all(item["status"] == "success" for item in result["results"]))
        self.assertEqual(set(self.repository.load()["quotes"]), {item.key for item in requests})

    def test_every_quote_has_source_and_times(self) -> None:
        self.service.refresh([MarketRequest("cn-index", "index", "000300")], self.provider)
        quote = self.repository.load()["quotes"]["cn-index"]

        self.assertEqual(quote["source"], "salarygo-m3-fixture")
        self.assertIn("as_of", quote)
        self.assertIn("fetched_at", quote)

    def test_one_failure_preserves_previous_cache_and_ledger(self) -> None:
        request = MarketRequest("cn-index", "index", "000300")
        self.service.refresh([request], self.provider)
        before = self.repository.load()["quotes"]["cn-index"]
        failing = StaticMarketDataProvider({"cn-index": {"error": "上游失败"}}, "failing-fixture")

        result = self.service.refresh([request], failing)
        after = self.repository.load()["quotes"]["cn-index"]
        applied = apply_quotes_to_ledger(example_ledger(), self.repository.snapshot())

        self.assertEqual(before, after)
        self.assertTrue(result["results"][0]["previous_preserved"])
        self.assertEqual(applied["holdings"][0]["current_price"], before["value"])

    def test_missing_refresh_does_not_clear_existing_holding_price(self) -> None:
        ledger = example_ledger()
        snapshot = {"quotes": {}}

        updated = apply_quotes_to_ledger(ledger, snapshot)

        self.assertEqual(updated["holdings"][0]["current_price"], ledger["holdings"][0]["current_price"])

    def test_stale_data_has_visible_warning(self) -> None:
        self.service.refresh(
            [MarketRequest("cn-index", "index", "000300")],
            self.provider,
            now=datetime(2026, 7, 18, tzinfo=timezone.utc),
        )
        snapshot = self.repository.snapshot(
            now=datetime(2026, 7, 25, tzinfo=timezone.utc),
            max_age_seconds={"index": 3600},
        )

        self.assertEqual(snapshot["quotes"]["cn-index"]["status"], "stale")
        self.assertEqual(snapshot["warnings"][0]["code"], "stale")


if __name__ == "__main__":
    unittest.main()
