"""User profile schema and deterministic validation for SalaryGo M1."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable
from uuid import UUID

CURRENT_SCHEMA_VERSION = 1

SUPPORTED_CURRENCIES = {"CNY", "SEK", "USD", "HKD"}
SUPPORTED_MARKETS = {"CN", "HK_CONNECT", "US", "CN_FUND"}
CRASH_BEHAVIORS = {"buy_more", "hold", "reduce", "unsure"}
EMERGENCY_STATUSES = {"adequate", "partial", "none"}


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ProfileValidationError(ValueError):
    """Raised when a profile cannot safely be persisted."""

    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = list(issues)
        summary = "; ".join(f"{i.path}: {i.message}" for i in self.issues)
        super().__init__(summary)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _required(mapping: Any, key: str, path: str, issues: list[ValidationIssue]) -> Any:
    if not isinstance(mapping, dict):
        return None
    if key not in mapping:
        issues.append(ValidationIssue(f"{path}.{key}".strip("."), "required", "必填字段缺失"))
        return None
    return mapping[key]


def _mapping(value: Any, path: str, issues: list[ValidationIssue]) -> dict[str, Any]:
    if not isinstance(value, dict):
        issues.append(ValidationIssue(path, "type", "必须是对象"))
        return {}
    return value


def _nonempty_string(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(ValidationIssue(path, "type", "必须是非空字符串"))


def _number_range(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    minimum: float,
    maximum: float,
    *,
    minimum_inclusive: bool = True,
) -> None:
    if not _is_number(value):
        issues.append(ValidationIssue(path, "type", "必须是数字"))
        return
    lower_invalid = value < minimum if minimum_inclusive else value <= minimum
    if lower_invalid or value > maximum:
        boundary = "≥" if minimum_inclusive else ">"
        issues.append(ValidationIssue(path, "range", f"必须 {boundary} {minimum} 且 ≤ {maximum}"))


def _enum(value: Any, path: str, allowed: set[str], issues: list[ValidationIssue]) -> None:
    if value not in allowed:
        issues.append(ValidationIssue(path, "enum", f"必须是以下值之一：{', '.join(sorted(allowed))}"))


def _iso_datetime(value: Any, path: str, issues: list[ValidationIssue], *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        issues.append(ValidationIssue(path, "datetime", "必须是带时区的 ISO 8601 时间"))
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        issues.append(ValidationIssue(path, "datetime", "必须是带时区的 ISO 8601 时间"))


def _string_list(value: Any, path: str, issues: list[ValidationIssue]) -> list[str]:
    if not isinstance(value, list):
        issues.append(ValidationIssue(path, "type", "必须是字符串数组"))
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            issues.append(ValidationIssue(f"{path}[{index}]", "type", "必须是非空字符串"))
        else:
            result.append(item)
    return result


def validate_profile(profile: Any) -> list[ValidationIssue]:
    """Return every structural and cross-field issue in a profile.

    An empty list means the profile is safe to save. Percentages are decimals:
    10% is represented as 0.10.
    """

    issues: list[ValidationIssue] = []
    root = _mapping(profile, "$", issues)
    if not isinstance(profile, dict):
        return issues

    schema_version = _required(root, "schema_version", "", issues)
    if schema_version != CURRENT_SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                "schema_version",
                "unsupported_schema",
                f"当前仅支持版本 {CURRENT_SCHEMA_VERSION}",
            )
        )

    profile_id = _required(root, "profile_id", "", issues)
    try:
        UUID(str(profile_id))
    except (ValueError, TypeError, AttributeError):
        issues.append(ValidationIssue("profile_id", "uuid", "必须是有效 UUID"))

    revision = _required(root, "revision", "", issues)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        issues.append(ValidationIssue("revision", "range", "必须是大于等于 0 的整数"))

    _iso_datetime(_required(root, "created_at", "", issues), "created_at", issues, nullable=True)
    _iso_datetime(_required(root, "updated_at", "", issues), "updated_at", issues, nullable=True)

    user = _mapping(_required(root, "user", "", issues), "user", issues)
    display_name = _required(user, "display_name", "user", issues)
    _nonempty_string(display_name, "user.display_name", issues)
    base_currency = _required(user, "base_currency", "user", issues)
    _enum(base_currency, "user.base_currency", SUPPORTED_CURRENCIES, issues)

    plan = _mapping(_required(root, "investment_plan", "", issues), "investment_plan", issues)
    horizon = _required(plan, "horizon_years", "investment_plan", issues)
    _number_range(horizon, "investment_plan.horizon_years", issues, 1, 50)
    target_return = _required(plan, "target_annual_return", "investment_plan", issues)
    _number_range(target_return, "investment_plan.target_annual_return", issues, 0, 0.5)
    max_drawdown = _required(plan, "max_drawdown", "investment_plan", issues)
    _number_range(max_drawdown, "investment_plan.max_drawdown", issues, 0, 1, minimum_inclusive=False)
    crash_behavior = _required(plan, "crash_behavior", "investment_plan", issues)
    _enum(crash_behavior, "investment_plan.crash_behavior", CRASH_BEHAVIORS, issues)
    allow_cash = _required(plan, "allow_cash", "investment_plan", issues)
    if not isinstance(allow_cash, bool):
        issues.append(ValidationIssue("investment_plan.allow_cash", "type", "必须是布尔值"))

    monthly = _mapping(_required(plan, "monthly_investment", "investment_plan", issues), "investment_plan.monthly_investment", issues)
    monthly_amount = _required(monthly, "amount", "investment_plan.monthly_investment", issues)
    _number_range(monthly_amount, "investment_plan.monthly_investment.amount", issues, 0, 1_000_000_000, minimum_inclusive=False)
    monthly_currency = _required(monthly, "currency", "investment_plan.monthly_investment", issues)
    _enum(monthly_currency, "investment_plan.monthly_investment.currency", SUPPORTED_CURRENCIES, issues)

    emergency = _mapping(_required(root, "emergency_fund", "", issues), "emergency_fund", issues)
    emergency_status = _required(emergency, "status", "emergency_fund", issues)
    _enum(emergency_status, "emergency_fund.status", EMERGENCY_STATUSES, issues)
    months_covered = _required(emergency, "months_covered", "emergency_fund", issues)
    _number_range(months_covered, "emergency_fund.months_covered", issues, 0, 60)

    planned_uses = _required(root, "planned_uses", "", issues)
    if not isinstance(planned_uses, list):
        issues.append(ValidationIssue("planned_uses", "type", "必须是数组"))
    else:
        for index, raw_use in enumerate(planned_uses):
            path = f"planned_uses[{index}]"
            use = _mapping(raw_use, path, issues)
            _nonempty_string(_required(use, "description", path, issues), f"{path}.description", issues)
            _number_range(_required(use, "amount", path, issues), f"{path}.amount", issues, 0, 1_000_000_000, minimum_inclusive=False)
            _enum(_required(use, "currency", path, issues), f"{path}.currency", SUPPORTED_CURRENCIES, issues)
            due_date = _required(use, "due_date", path, issues)
            try:
                date.fromisoformat(due_date)
            except (ValueError, TypeError):
                issues.append(ValidationIssue(f"{path}.due_date", "date", "必须是 YYYY-MM-DD 日期"))

    accounts = _required(root, "accounts", "", issues)
    account_ids: set[str] = set()
    account_currencies: set[str] = set()
    account_markets: set[str] = set()
    if not isinstance(accounts, list) or not accounts:
        issues.append(ValidationIssue("accounts", "required", "至少需要一个投资账户"))
    else:
        for index, raw_account in enumerate(accounts):
            path = f"accounts[{index}]"
            account = _mapping(raw_account, path, issues)
            account_id = _required(account, "id", path, issues)
            _nonempty_string(account_id, f"{path}.id", issues)
            if isinstance(account_id, str):
                if account_id in account_ids:
                    issues.append(ValidationIssue(f"{path}.id", "duplicate", "账户 ID 不得重复"))
                account_ids.add(account_id)
            _nonempty_string(_required(account, "name", path, issues), f"{path}.name", issues)
            markets = _string_list(_required(account, "markets", path, issues), f"{path}.markets", issues)
            if not markets:
                issues.append(ValidationIssue(f"{path}.markets", "required", "至少需要一个可购买市场"))
            for market in markets:
                _enum(market, f"{path}.markets", SUPPORTED_MARKETS, issues)
                account_markets.add(market)
            currencies = _string_list(_required(account, "currencies", path, issues), f"{path}.currencies", issues)
            if not currencies:
                issues.append(ValidationIssue(f"{path}.currencies", "required", "至少需要一种账户币种"))
            for currency in currencies:
                _enum(currency, f"{path}.currencies", SUPPORTED_CURRENCIES, issues)
                account_currencies.add(currency)

    funding_currencies = _string_list(_required(root, "funding_currencies", "", issues), "funding_currencies", issues)
    if not funding_currencies:
        issues.append(ValidationIssue("funding_currencies", "required", "至少需要一种投入币种"))
    for currency in funding_currencies:
        _enum(currency, "funding_currencies", SUPPORTED_CURRENCIES, issues)

    limits = _mapping(_required(root, "concentration_limits", "", issues), "concentration_limits", issues)
    limit_values: dict[str, Any] = {}
    for key in ("single_stock", "individual_stocks_total", "market", "sector", "currency"):
        value = _required(limits, key, "concentration_limits", issues)
        limit_values[key] = value
        _number_range(value, f"concentration_limits.{key}", issues, 0, 1, minimum_inclusive=False)

    exclusions = _mapping(_required(root, "exclusions", "", issues), "exclusions", issues)
    for key in ("asset_types", "industries", "companies", "products"):
        _string_list(_required(exclusions, key, "exclusions", issues), f"exclusions.{key}", issues)

    # Deterministic cross-field checks. These prevent the Agent from silently
    # turning an internally inconsistent onboarding answer into a strategy.
    if _is_number(target_return) and _is_number(max_drawdown):
        if target_return >= 0.10 and max_drawdown < 0.15:
            issues.append(
                ValidationIssue(
                    "investment_plan",
                    "return_drawdown_conflict",
                    "目标年化收益率不低于 10%，但最大可接受回撤低于 15%；请调整目标或风险边界",
                )
            )
        elif target_return >= 0.15 and max_drawdown < 0.25:
            issues.append(
                ValidationIssue(
                    "investment_plan",
                    "return_drawdown_conflict",
                    "目标年化收益率不低于 15%，但最大可接受回撤低于 25%；请调整目标或风险边界",
                )
            )
    if emergency_status == "none" and allow_cash is False:
        issues.append(
            ValidationIssue(
                "investment_plan.allow_cash",
                "liquidity_conflict",
                "应急资金未建立时不能禁止暂留现金",
            )
        )
    if _is_number(limit_values.get("single_stock")) and _is_number(limit_values.get("individual_stocks_total")):
        if limit_values["single_stock"] > limit_values["individual_stocks_total"]:
            issues.append(
                ValidationIssue(
                    "concentration_limits",
                    "stock_limit_conflict",
                    "单只个股上限不能高于个股合计上限",
                )
            )
    if isinstance(monthly_currency, str) and monthly_currency not in funding_currencies:
        issues.append(
            ValidationIssue(
                "investment_plan.monthly_investment.currency",
                "currency_conflict",
                "常规投入币种必须包含在 funding_currencies 中",
            )
        )
    unavailable = set(funding_currencies) - account_currencies
    if unavailable:
        issues.append(
            ValidationIssue(
                "funding_currencies",
                "account_currency_conflict",
                f"以下投入币种没有对应账户支持：{', '.join(sorted(unavailable))}",
            )
        )
    if _is_number(limit_values.get("currency")) and funding_currencies:
        if limit_values["currency"] * len(set(funding_currencies)) < 1:
            issues.append(
                ValidationIssue(
                    "concentration_limits.currency",
                    "currency_limit_conflict",
                    "币种上限与可用币种数量组合后无法达到 100%",
                )
            )
    if _is_number(limit_values.get("market")) and account_markets:
        if limit_values["market"] * len(account_markets) < 1:
            issues.append(
                ValidationIssue(
                    "concentration_limits.market",
                    "market_limit_conflict",
                    "市场上限与可用市场数量组合后无法达到 100%",
                )
            )

    # Deduplicate identical issues caused by a missing parent and child checks.
    unique: list[ValidationIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.path, issue.code, issue.message)
        if key not in seen:
            unique.append(issue)
            seen.add(key)
    return unique


def assert_valid_profile(profile: Any) -> None:
    issues = validate_profile(profile)
    if issues:
        raise ProfileValidationError(issues)
