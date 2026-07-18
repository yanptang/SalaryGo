from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from salarygo.allocation import evaluate_sell_triggers, generate_allocation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def allocation_fixture() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "allocation.fixture.json").read_text(encoding="utf-8"))


class AllocationTests(unittest.TestCase):
    def test_money_is_conserved_per_funding_currency(self) -> None:
        plan = generate_allocation(allocation_fixture())
        totals: dict[str, float] = {}
        for item in plan["recommendations"]:
            totals[item["funding_currency"]] = totals.get(item["funding_currency"], 0) + item["amount"]

        self.assertEqual(plan["status"], "formal")
        self.assertEqual(totals, {"CNY": 15000.0, "SEK": 5000.0})
        self.assertTrue(plan["risk_validation"]["passed"])

    def test_hard_instrument_limit_is_never_exceeded(self) -> None:
        context = allocation_fixture()
        context["limits"]["single_instrument"] = 0.35
        plan = generate_allocation(context)

        self.assertEqual(plan["status"], "formal")
        self.assertTrue(all(value / plan["post_total_base"] <= 0.35 + 1e-9 for value in plan["post_instrument_values"].values()))

    def test_no_meaningless_small_buy_is_emitted(self) -> None:
        context = allocation_fixture()
        for candidate in context["candidates"]:
            if candidate["bucket"] != "cash":
                candidate["minimum_trade"] = 20000
        plan = generate_allocation(context)

        self.assertTrue(all(item["operation"] == "hold_cash" for item in plan["recommendations"]))

    def test_all_ineligible_candidates_retain_all_cash(self) -> None:
        context = allocation_fixture()
        for candidate in context["candidates"]:
            if candidate["bucket"] != "cash":
                candidate["eligible"] = False
        plan = generate_allocation(context)

        self.assertEqual(plan["status"], "formal")
        self.assertTrue(all(item["operation"] == "hold_cash" for item in plan["recommendations"]))
        self.assertIn("全部保留现金", plan["warnings"][0])

    def test_stale_data_blocks_formal_advice(self) -> None:
        context = allocation_fixture()
        context["candidates"][0]["data_fresh"] = False
        plan = generate_allocation(context)

        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["recommendations"], [])
        self.assertEqual(plan["errors"][0]["code"], "stale_data")

    def test_ordinary_temperature_change_does_not_trigger_sell(self) -> None:
        triggers = evaluate_sell_triggers({"id": "holding-1"}, [{"type": "temperature_change", "active": True}])
        red_line = evaluate_sell_triggers({"id": "holding-1"}, [{"type": "fundamental_red_line", "active": True}])

        self.assertEqual(triggers, [])
        self.assertEqual(red_line[0]["code"], "fundamental_red_line")

    def test_same_input_is_reproducible(self) -> None:
        context = allocation_fixture()
        self.assertEqual(generate_allocation(context), generate_allocation(copy.deepcopy(context)))


if __name__ == "__main__":
    unittest.main()

