"""M9 deterministic cash-flow backtesting with no-lookahead signals."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import date
from typing import Any

BACKTEST_VERSION = "backtest-v1"
DISCLAIMER = "历史模拟不代表未来收益，也不保证实现目标年化收益率。"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _weights(strategy: str, base: dict[str, float], temperature: float | None) -> dict[str, float]:
    result = deepcopy(base)
    if strategy in {"temperature_tilt", "temperature_defensive"} and temperature is not None:
        adjustment = _clamp((50 - temperature) / 100 * 0.20, -0.10, 0.10)
        result["index_core"] += adjustment
        result["cash"] -= adjustment
    if strategy == "temperature_defensive" and temperature is not None and temperature >= 80:
        reduction = min(0.15, result["index_core"])
        result["index_core"] -= reduction
        result["defensive_bond"] += reduction * 0.7
        result["cash"] += reduction * 0.3
    total = sum(result.values())
    return {key: value / total for key, value in result.items()}


def _latest_signal(signals: list[dict[str, Any]], period_date: str) -> tuple[float | None, str | None]:
    current = date.fromisoformat(period_date)
    available = [item for item in signals if date.fromisoformat(item["published_at"]) <= current]
    if not available:
        return None, None
    available.sort(key=lambda item: item["published_at"])
    return float(available[-1]["temperature"]), available[-1]["published_at"]


def _max_drawdown(values: list[float]) -> tuple[float, int]:
    high = 0.0
    maximum = 0.0
    start = 0
    worst_duration = 0
    for index, value in enumerate(values):
        if value >= high:
            high = value
            start = index
        if high:
            maximum = min(maximum, value / high - 1)
            worst_duration = max(worst_duration, index - start)
    return maximum, worst_duration


def _annualized_volatility(monthly_returns: list[float]) -> float:
    if len(monthly_returns) < 2:
        return 0.0
    mean = sum(monthly_returns) / len(monthly_returns)
    variance = sum((value - mean) ** 2 for value in monthly_returns) / (len(monthly_returns) - 1)
    return math.sqrt(variance) * math.sqrt(12)


def _rolling_compound(values: list[float], months: int) -> list[float]:
    results: list[float] = []
    for end in range(months, len(values) + 1):
        compound = 1.0
        for value in values[end - months:end]:
            compound *= 1 + value
        results.append(compound - 1)
    return results


def _money_weighted_return(contribution: float, months: int, ending_value: float) -> float | None:
    if months <= 0 or ending_value <= 0:
        return None
    cashflows = [-contribution] * months + [ending_value]

    def npv(monthly_rate: float) -> float:
        return sum(value / ((1 + monthly_rate) ** index) for index, value in enumerate(cashflows))

    low, high = -0.99, 1.0
    if npv(low) * npv(high) > 0:
        return None
    for _ in range(120):
        midpoint = (low + high) / 2
        if npv(low) * npv(midpoint) <= 0:
            high = midpoint
        else:
            low = midpoint
    monthly = (low + high) / 2
    return (1 + monthly) ** 12 - 1


def simulate_strategy(config: dict[str, Any], strategy: str, *, include_fees: bool) -> dict[str, Any]:
    holdings = {bucket: 0.0 for bucket in config["base_weights"]}
    values: list[float] = []
    nav_values: list[float] = []
    monthly_portfolio_returns: list[float] = []
    signal_audit: list[dict[str, Any]] = []
    total_fees = 0.0
    contribution = float(config["monthly_contribution"])
    fee_rate = float(config.get("transaction_fee_rate", 0)) if include_fees else 0.0

    for period in config["periods"]:
        temperature, published_at = _latest_signal(config.get("signals", []), period["date"])
        weights = _weights(strategy, config["base_weights"], temperature)
        before = sum(holdings.values())
        available = contribution
        contribution_fee = available * fee_rate
        total_fees += contribution_fee
        available -= contribution_fee
        for bucket in holdings:
            holdings[bucket] += available * weights[bucket]
        # Rebalance after contribution; fees are charged on one-way turnover.
        total_before_rebalance = sum(holdings.values())
        target_values = {bucket: total_before_rebalance * weights[bucket] for bucket in holdings}
        turnover = sum(abs(holdings[bucket] - target_values[bucket]) for bucket in holdings) / 2
        rebalance_fee = turnover * fee_rate
        total_fees += rebalance_fee
        if total_before_rebalance:
            scale = (total_before_rebalance - rebalance_fee) / total_before_rebalance
            holdings = {bucket: target_values[bucket] * scale for bucket in holdings}
        start_invested = sum(holdings.values())
        for bucket in holdings:
            holdings[bucket] *= 1 + float(period["returns"][bucket])
        ending = sum(holdings.values())
        monthly_return = ending / start_invested - 1 if start_invested else 0.0
        monthly_portfolio_returns.append(monthly_return)
        nav_values.append((nav_values[-1] if nav_values else 1.0) * (1 + monthly_return))
        values.append(ending)
        signal_audit.append({"period": period["date"], "temperature": temperature, "signal_published_at": published_at})

    drawdown, recovery_months = _max_drawdown(nav_values)
    rolling_12 = _rolling_compound(monthly_portfolio_returns, 12)
    rolling_36 = _rolling_compound(monthly_portfolio_returns, 36)
    rolling_60 = _rolling_compound(monthly_portfolio_returns, 60)
    ending = values[-1] if values else 0.0
    return {
        "strategy": strategy,
        "include_fees": include_fees,
        "ending_value": round(ending, 8),
        "total_contributions": round(contribution * len(config["periods"]), 8),
        "total_fees": round(total_fees, 8),
        "money_weighted_annual_return": _money_weighted_return(contribution, len(config["periods"]), ending),
        "annualized_volatility": _annualized_volatility(monthly_portfolio_returns),
        "maximum_drawdown": drawdown,
        "drawdown_duration_months": recovery_months,
        "worst_month": min(monthly_portfolio_returns) if monthly_portfolio_returns else None,
        "worst_rolling_12m": min(rolling_12) if rolling_12 else None,
        "rolling_3y": rolling_36,
        "rolling_5y": rolling_60,
        "values": [round(value, 8) for value in values],
        "monthly_returns": monthly_portfolio_returns,
        "signal_audit": signal_audit,
    }


def run_backtest(config: dict[str, Any]) -> dict[str, Any]:
    strategies = ["fixed_dca", "fixed_target", "temperature_tilt", "temperature_defensive"]
    results = []
    for strategy in strategies:
        results.append({
            "gross": simulate_strategy(config, strategy, include_fees=False),
            "net": simulate_strategy(config, strategy, include_fees=True),
        })
    benchmark = results[0]["net"]["ending_value"]
    for result in results:
        result["net"]["vs_fixed_dca"] = round(result["net"]["ending_value"] - benchmark, 8)
    return {
        "backtest_version": BACKTEST_VERSION,
        "data_as_of": config["data_as_of"],
        "training_end": config.get("training_end"),
        "out_of_sample_start": config.get("out_of_sample_start"),
        "results": results,
        "disclaimer": DISCLAIMER,
    }


def render_backtest_markdown(report: dict[str, Any]) -> str:
    lines = ["# SalaryGo 策略回测报告", "", f"> {report['disclaimer']}", "", f"数据截至：{report['data_as_of']}", "", "## 策略比较", ""]
    lines.append("| 策略 | 费用后期末价值 | 费用 | 最大回撤 | 相对固定定投 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for result in report["results"]:
        net = result["net"]
        lines.append(
            f"| {net['strategy']} | {net['ending_value']:.2f} | {net['total_fees']:.2f} | "
            f"{net['maximum_drawdown']:.2%} | {net['vs_fixed_dca']:.2f} |"
        )
    lines.extend(["", "## 失败时期", ""])
    for result in report["results"]:
        net = result["net"]
        lines.append(f"- {net['strategy']}：最差单月 {net['worst_month']:.2%}，最差滚动 12 个月 {net['worst_rolling_12m'] if net['worst_rolling_12m'] is not None else '样本不足'}")
    lines.extend(["", "所有信号均按 `published_at <= 投资日期` 使用；报告保留逐月信号审计记录。", ""])
    return "\n".join(lines)
