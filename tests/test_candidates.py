from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from salarygo.candidates import CandidatePoolRepository, select_products

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def candidate_fixture() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "candidates.fixture.json").read_text(encoding="utf-8"))


class CandidatePoolTests(unittest.TestCase):
    def selection(self, fixture: dict | None = None) -> dict:
        value = fixture or candidate_fixture()
        return select_products(
            value["slots"], value["products"], planned_amounts=value["planned_amounts"], as_of=value["as_of"]
        )

    def test_same_input_has_reproducible_ranking_independent_of_input_order(self) -> None:
        fixture = candidate_fixture()
        first = self.selection(fixture)
        fixture["products"].reverse()
        second = self.selection(fixture)

        self.assertEqual(first, second)
        self.assertEqual(first["assignments"][0]["main"]["product_id"], "fund-a")
        self.assertEqual(first["assignments"][0]["backup"]["product_id"], "fund-b")

    def test_limited_high_premium_and_missing_data_are_excluded(self) -> None:
        result = self.selection()
        exclusions = {item["product_id"]: {reason["code"] for reason in item["reasons"]} for item in result["exclusions"]}

        self.assertIn("insufficient_limit", exclusions["fund-limited"])
        self.assertIn("high_premium", exclusions["fund-premium"])
        self.assertIn("missing_data", exclusions["fund-missing"])

    def test_main_product_has_score_components_and_selection_reason(self) -> None:
        assignment = self.selection()["assignments"][0]

        self.assertIn("实施质量得分最高", assignment["selection_reason"])
        self.assertEqual(
            set(assignment["main"]["score"]["components"]),
            {"tracking", "cost", "scale", "liquidity", "history", "operability"},
        )

    def test_pool_change_and_unchanged_review_both_have_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidatePoolRepository(directory)
            selection = self.selection()
            first = repository.record_review(selection, reason="initial", expected_revision=0)
            second = repository.record_review(selection, reason="scheduled_quarterly", expected_revision=1)
            changed = json.loads(json.dumps(selection))
            changed["assignments"][0]["main"], changed["assignments"][0]["backup"] = (
                changed["assignments"][0]["backup"], changed["assignments"][0]["main"]
            )
            third = repository.record_review(changed, reason="product_change", expected_revision=2)

        self.assertEqual(first["pool_version"], 1)
        self.assertEqual(second["pool_version"], 1)
        self.assertFalse(second["history"][-1]["changed"])
        self.assertEqual(third["pool_version"], 2)
        self.assertEqual(len(third["history"]), 3)


if __name__ == "__main__":
    unittest.main()

