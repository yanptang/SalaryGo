from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from salarygo.profile import validate_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def example_profile() -> dict:
    return json.loads((PROJECT_ROOT / "examples" / "profile.example.json").read_text(encoding="utf-8"))


class ProfileValidationTests(unittest.TestCase):
    def test_empty_profile_reports_all_required_fields(self) -> None:
        issues = validate_profile({})

        self.assertGreater(len(issues), 0)
        self.assertTrue(any(issue.path == "schema_version" and issue.code == "required" for issue in issues))

    def test_example_profile_is_valid(self) -> None:
        self.assertEqual(validate_profile(example_profile()), [])

    def test_missing_required_field_has_exact_path(self) -> None:
        profile = example_profile()
        del profile["investment_plan"]["horizon_years"]

        issues = validate_profile(profile)

        self.assertTrue(
            any(issue.path == "investment_plan.horizon_years" and issue.code == "required" for issue in issues)
        )

    def test_return_and_drawdown_conflict_is_detected(self) -> None:
        profile = example_profile()
        profile["investment_plan"]["target_annual_return"] = 0.15
        profile["investment_plan"]["max_drawdown"] = 0.1

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("return_drawdown_conflict", codes)

    def test_missing_emergency_fund_and_no_cash_conflict(self) -> None:
        profile = example_profile()
        profile["emergency_fund"]["status"] = "none"
        profile["emergency_fund"]["months_covered"] = 0
        profile["investment_plan"]["allow_cash"] = False

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("liquidity_conflict", codes)

    def test_single_stock_cannot_exceed_total_stock_limit(self) -> None:
        profile = example_profile()
        profile["concentration_limits"]["single_stock"] = 0.2
        profile["concentration_limits"]["individual_stocks_total"] = 0.1

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("stock_limit_conflict", codes)

    def test_funding_currency_must_be_supported_by_an_account(self) -> None:
        profile = example_profile()
        profile["funding_currencies"].append("USD")
        for account in profile["accounts"]:
            account["currencies"] = [currency for currency in account["currencies"] if currency != "USD"]

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("account_currency_conflict", codes)

    def test_limits_must_be_mathematically_possible(self) -> None:
        profile = example_profile()
        profile["concentration_limits"]["currency"] = 0.4
        profile["concentration_limits"]["market"] = 0.2

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("currency_limit_conflict", codes)
        self.assertIn("market_limit_conflict", codes)

    def test_unknown_schema_is_rejected(self) -> None:
        profile = example_profile()
        profile["schema_version"] = 99

        codes = {issue.code for issue in validate_profile(profile)}

        self.assertIn("unsupported_schema", codes)

    def test_boolean_is_not_accepted_as_number(self) -> None:
        profile = example_profile()
        profile["investment_plan"]["horizon_years"] = True

        issues = validate_profile(profile)

        self.assertTrue(any(issue.path == "investment_plan.horizon_years" and issue.code == "type" for issue in issues))

    def test_validation_does_not_mutate_input(self) -> None:
        profile = example_profile()
        original = copy.deepcopy(profile)

        validate_profile(profile)

        self.assertEqual(profile, original)


if __name__ == "__main__":
    unittest.main()
