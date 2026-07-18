from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from salarygo.ledger import LedgerRepository, value_portfolio
from salarygo.market import MarketCacheRepository, MarketRefreshService, MarketRequest, StaticMarketDataProvider, apply_quotes_to_ledger
from tests.test_ledger import example_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ModuleIntegrationTests(unittest.TestCase):
    def test_market_refresh_updates_ledger_without_overwriting_user_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_repository = MarketCacheRepository(directory)
            provider = StaticMarketDataProvider.from_file(PROJECT_ROOT / "examples" / "market.fixture.json")
            MarketRefreshService(market_repository).refresh([
                MarketRequest("cn-index", "index", "000300"),
                MarketRequest("hk-stock", "stock", "HK-DEMO"),
                MarketRequest("us-etf", "fund", "US-DEMO"),
            ], provider)
            original = example_ledger()
            updated = apply_quotes_to_ledger(original, market_repository.snapshot())
            ledger_repository = LedgerRepository(directory)
            ledger_repository.save(updated, expected_revision=0)
            valued = value_portfolio(ledger_repository.load(), {"HKD": 0.9, "USD": 7.2})

        self.assertEqual(original["holdings"][2]["current_price"], None)
        self.assertEqual(updated["holdings"][2]["current_price"], 101.0)
        self.assertTrue(valued["complete"])
        self.assertEqual(valued["known_market_value"], 9517.0)


if __name__ == "__main__":
    unittest.main()
