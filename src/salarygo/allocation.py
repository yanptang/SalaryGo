"""M6 deterministic new-money allocation and hard risk validation."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime
from typing import Any

ALLOCATION_RULE_VERSION = "allocation-v1"
RISK_RULE_VERSION = "risk-v1"
BUCKETS = ("cash", "defensive_bond", "index_core", "individual_stock")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _money_cents(amount: float) -> int:
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount <= 0:
        raise ValueError("新增资金必须是大于 0 的有限数字")
    return int(round(amount * 100))


def _candidate_order(candidate: dict[str, Any]) -> tuple[float, str]:
    temperature = candidate.get("temperature")
    attractiveness = 50 if not isinstance(temperature, (int, float)) else 100 - temperature
    return (-attractiveness, candidate["id"])


def validate_allocation(plan: dict[str, Any], context: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    expected = {item["currency"]: round(float(item["amount"]), 2) for item in context["new_funds"]}
    actual: dict[str, float] = {}
    for item in plan.get("recommendations", []):
        actual[item["funding_currency"]] = round(actual.get(item["funding_currency"], 0) + item["amount"], 2)
        if item["amount"] < item.get("minimum_trade", 0) and item["operation"] != "hold_cash":
            errors.append({"code": "minimum_trade", "message": f"{item['asset_id']} 低于最小交易金额"})
    if actual != expected:
        errors.append({"code": "money_conservation", "message": f"分配金额 {actual} 与输入 {expected} 不一致"})

    post_total = plan.get("post_total_base", 0)
    if post_total <= 0:
        errors.append({"code": "invalid_total", "message": "操作后组合总额无效"})
        return errors
    for asset_id, value in plan.get("post_instrument_values", {}).items():
        if value / post_total > context["limits"]["single_instrument"] + 1e-9:
            errors.append({"code": "instrument_limit", "message": f"{asset_id} 超过单一标的上限"})
    stock_total = plan.get("post_bucket_values", {}).get("individual_stock", 0)
    if stock_total / post_total > context["limits"]["individual_stocks_total"] + 1e-9:
        errors.append({"code": "stock_total_limit", "message": "个股合计超过上限"})
    for market, value in plan.get("post_market_values", {}).items():
        if value / post_total > context["limits"]["market"] + 1e-9:
            errors.append({"code": "market_limit", "message": f"市场 {market} 超过上限"})
    for currency, value in plan.get("post_currency_values", {}).items():
        if value / post_total > context["limits"]["currency"] + 1e-9:
            errors.append({"code": "currency_limit", "message": f"币种 {currency} 超过上限"})
    return errors


def generate_allocation(context: dict[str, Any]) -> dict[str, Any]:
    """Allocate only new cash. On any hard-risk failure, return no formal advice."""
    required = ("as_of", "new_funds", "fx_to_base", "current", "target_ranges", "limits", "candidates")
    missing = [key for key in required if key not in context]
    if missing:
        return {"status": "blocked", "recommendations": [], "errors": [{"code": "missing_input", "message": ", ".join(missing)}]}
    try:
        _parse_time(context["as_of"])
        funds = [(item["currency"], _money_cents(item["amount"])) for item in context["new_funds"]]
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "blocked", "recommendations": [], "errors": [{"code": "invalid_input", "message": str(exc)}]}

    rates = context["fx_to_base"]
    for currency, _ in funds:
        if currency not in rates or rates[currency] <= 0:
            return {"status": "blocked", "recommendations": [], "errors": [{"code": "missing_fx", "message": f"缺少 {currency} 汇率"}]}
    stale = [item["id"] for item in context["candidates"] if item.get("eligible") and not item.get("data_fresh")]
    if stale:
        return {"status": "blocked", "recommendations": [], "errors": [{"code": "stale_data", "message": f"候选数据过期：{', '.join(stale)}"}]}

    current = deepcopy(context["current"])
    bucket_values = {bucket: float(current.get("bucket_values", {}).get(bucket, 0)) for bucket in BUCKETS}
    instrument_values = {key: float(value) for key, value in current.get("instrument_values", {}).items()}
    market_values = {key: float(value) for key, value in current.get("market_values", {}).items()}
    currency_values = {key: float(value) for key, value in current.get("currency_values", {}).items()}
    current_total = sum(bucket_values.values())
    new_total_base = sum(cents / 100 * rates[currency] for currency, cents in funds)
    post_total = current_total + new_total_base
    recommendations: list[dict[str, Any]] = []

    eligible = [item for item in context["candidates"] if item.get("eligible") and item.get("confidence") != "stopped"]
    non_cash_eligible = [item for item in eligible if item.get("bucket") != "cash"]
    all_risk_assets_unavailable = not non_cash_eligible

    for currency, fund_cents in funds:
        remaining = fund_cents
        rate = rates[currency]
        candidates = [item for item in eligible if currency in item.get("funding_currencies", []) and item.get("bucket") != "cash"]
        bucket_priorities: list[tuple[float, str]] = []
        for bucket in ("defensive_bond", "index_core", "individual_stock"):
            target = context["target_ranges"][bucket]
            midpoint = (target["min"] + target["max"]) / 2
            gap = max(0.0, midpoint * post_total - bucket_values[bucket])
            bucket_priorities.append((-gap, bucket))
        bucket_priorities.sort()

        for _, bucket in bucket_priorities:
            bucket_candidates = sorted([item for item in candidates if item["bucket"] == bucket], key=_candidate_order)
            if not bucket_candidates or remaining <= 0:
                continue
            target = context["target_ranges"][bucket]
            midpoint = (target["min"] + target["max"]) / 2
            desired_base = max(0.0, midpoint * post_total - bucket_values[bucket])
            headroom_base = max(0.0, target["max"] * post_total - bucket_values[bucket])
            budget_base = min(desired_base, headroom_base, remaining / 100 * rate)
            for candidate in bucket_candidates:
                if budget_base <= 0 or remaining <= 0:
                    break
                current_instrument = instrument_values.get(candidate["id"], 0.0)
                instrument_headroom = max(0.0, context["limits"]["single_instrument"] * post_total - current_instrument)
                if bucket == "individual_stock":
                    stock_headroom = max(0.0, context["limits"]["individual_stocks_total"] * post_total - bucket_values[bucket])
                    instrument_headroom = min(instrument_headroom, stock_headroom)
                amount_cents = min(remaining, int(math.floor(min(budget_base, instrument_headroom) / rate * 100 + 1e-9)))
                minimum_cents = int(round(candidate.get("minimum_trade", 0) * 100))
                if amount_cents < minimum_cents:
                    continue
                amount = amount_cents / 100
                base_value = amount * rate
                recommendations.append({
                    "asset_id": candidate["id"], "name": candidate["name"], "operation": "buy",
                    "bucket": bucket, "platform": candidate["platform"], "market": candidate["market"],
                    "asset_currency": candidate["asset_currency"], "funding_currency": currency,
                    "amount": amount, "base_value": round(base_value, 8), "minimum_trade": candidate.get("minimum_trade", 0),
                    "reason_codes": ["bucket_underweight", "candidate_eligible", "new_money_rebalance"],
                    "temperature": candidate.get("temperature"), "confidence": candidate.get("confidence"),
                })
                remaining -= amount_cents
                budget_base -= base_value
                bucket_values[bucket] += base_value
                instrument_values[candidate["id"]] = current_instrument + base_value
                market_values[candidate["market"]] = market_values.get(candidate["market"], 0) + base_value
                currency_values[candidate["asset_currency"]] = currency_values.get(candidate["asset_currency"], 0) + base_value

        if remaining:
            amount = remaining / 100
            base_value = amount * rate
            cash_candidates = sorted(
                [item for item in eligible if item.get("bucket") == "cash" and currency in item.get("funding_currencies", [])],
                key=lambda item: item["id"],
            )
            cash = cash_candidates[0] if cash_candidates else {
                "id": f"unallocated-{currency}", "name": f"暂留 {currency} 现金", "platform": "unallocated",
                "market": "CASH", "asset_currency": currency,
            }
            recommendations.append({
                "asset_id": cash["id"], "name": cash["name"], "operation": "hold_cash", "bucket": "cash",
                "platform": cash["platform"], "market": cash["market"], "asset_currency": cash["asset_currency"],
                "funding_currency": currency, "amount": amount, "base_value": round(base_value, 8), "minimum_trade": 0,
                "reason_codes": ["all_candidates_ineligible" if all_risk_assets_unavailable else "residual_or_no_eligible_capacity"],
                "temperature": None, "confidence": "high",
            })
            bucket_values["cash"] += base_value
            instrument_values[cash["id"]] = instrument_values.get(cash["id"], 0) + base_value
            market_values["CASH"] = market_values.get("CASH", 0) + base_value
            currency_values[currency] = currency_values.get(currency, 0) + base_value

    draft = {
        "status": "formal",
        "as_of": context["as_of"],
        "allocation_rule_version": ALLOCATION_RULE_VERSION,
        "risk_rule_version": RISK_RULE_VERSION,
        "base_currency": context["base_currency"],
        "input_funds": context["new_funds"],
        "recommendations": recommendations,
        "post_total_base": round(post_total, 8),
        "post_bucket_values": {key: round(value, 8) for key, value in bucket_values.items()},
        "post_instrument_values": {key: round(value, 8) for key, value in instrument_values.items()},
        "post_market_values": {key: round(value, 8) for key, value in market_values.items()},
        "post_currency_values": {key: round(value, 8) for key, value in currency_values.items()},
        "warnings": (["没有风险资产满足条件，本次新增资金全部保留现金"] if all_risk_assets_unavailable else []),
    }
    errors = validate_allocation(draft, context)
    if errors:
        return {
            "status": "blocked", "recommendations": [], "errors": errors,
            "allocation_rule_version": ALLOCATION_RULE_VERSION, "risk_rule_version": RISK_RULE_VERSION,
        }
    draft["risk_validation"] = {"passed": True, "errors": []}
    return draft


def evaluate_sell_triggers(position: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Ordinary temperature changes never trigger a sale."""
    allowed = {
        "fundamental_red_line": "个股投资逻辑或经营红线触发",
        "concentration_breach": "仓位超过硬性上限",
        "product_deterioration": "产品清盘、长期限购、跟踪或流动性恶化",
        "user_horizon_change": "用户资金用途或投资期限变化",
        "rebalance_threshold": "定期再平衡达到预设阈值",
    }
    triggers: list[dict[str, str]] = []
    for event in events:
        event_type = event.get("type")
        if event_type in allowed and event.get("active"):
            triggers.append({"code": event_type, "message": allowed[event_type], "position_id": position["id"]})
    return triggers

