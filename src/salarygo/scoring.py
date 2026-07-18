"""M5 point-in-time index temperature and watchlist-stock scoring."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .storage import RevisionConflictError, default_data_dir

INDEX_SCORING_VERSION = "index-temperature-v1"
STOCK_SCORING_VERSION = "watchlist-stock-v1"
SCORE_STORE_SCHEMA_VERSION = 1
RED_LINE_TYPES = {"adverse_audit", "delisting", "major_violation", "going_concern"}


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _linear(value: float, low: float, high: float) -> float:
    if high == low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100)


def _latest_available(records: list[dict[str, Any]], field: str, as_of: datetime) -> tuple[dict[str, Any] | None, int]:
    available = [record for record in records if _time(record[field]) <= as_of]
    future_count = len(records) - len(available)
    available.sort(key=lambda record: _time(record[field]))
    return (available[-1] if available else None, future_count)


def score_index_temperature(dataset: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    """Return 0=cold and 100=hot using point-in-time data only."""
    cutoff = _time(as_of)
    prices = [record for record in dataset.get("prices", []) if _time(record["as_of"]) <= cutoff]
    prices.sort(key=lambda record: _time(record["as_of"]))
    future_prices = len(dataset.get("prices", [])) - len(prices)
    metric, future_metrics = _latest_available(dataset.get("metrics", []), "published_at", cutoff)
    components: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    used_sources = {record.get("source") for record in prices if record.get("source")}
    if metric and metric.get("source"):
        used_sources.add(metric["source"])
    source_complete = all(record.get("source") for record in prices) and (metric is None or bool(metric.get("source")))
    if not source_complete:
        warnings.append("部分指数评分数据缺少来源")

    if prices:
        current = prices[-1]
        current_price = float(current["close"])
        for label, days in (("range_1y", 365), ("range_3y", 1095), ("range_5y", 1825)):
            start = cutoff - timedelta(days=days)
            window = [record for record in prices if _time(record["as_of"]) >= start]
            if len(window) >= 2:
                values = [float(record["close"]) for record in window]
                position = 50.0 if max(values) == min(values) else (current_price - min(values)) / (max(values) - min(values)) * 100
                components[label] = {
                    "score": round(_clamp(position), 6),
                    "raw": round(position / 100, 8),
                    "explanation": f"当前价格位于近 {label[6:]} 区间的 {position:.1f}%",
                }
            else:
                warnings.append(f"{label} 历史价格不足")
        five_year = [record for record in prices if _time(record["as_of"]) >= cutoff - timedelta(days=1825)]
        if five_year:
            high = max(float(record["close"]) for record in five_year)
            drawdown = current_price / high - 1
            score = _clamp(100 - abs(min(drawdown, 0)) / 0.5 * 100)
            components["drawdown"] = {
                "score": round(score, 6),
                "raw": round(drawdown, 8),
                "explanation": f"距离五年窗口高点回撤 {drawdown:.1%}",
            }

        def prior_price(days: int) -> float | None:
            target = cutoff - timedelta(days=days)
            eligible = [record for record in prices if _time(record["as_of"]) <= target]
            if not eligible or target - _time(eligible[-1]["as_of"]) > timedelta(days=120):
                return None
            return float(eligible[-1]["close"])

        returns: list[float] = []
        for label, days, low, high in (("trend_6m", 182, -0.20, 0.30), ("trend_12m", 365, -0.30, 0.50)):
            base = prior_price(days)
            if base:
                value = current_price / base - 1
                returns.append(value)
                components[label] = {
                    "score": round(_linear(value, low, high), 6),
                    "raw": round(value, 8),
                    "explanation": f"区间收益 {value:.1%}",
                }
            else:
                warnings.append(f"{label} 基准价格不足")
        short_base = prior_price(20)
        if short_base:
            short_return = current_price / short_base - 1
            risk_temperature = _clamp(40 + max(0, -short_return) / 0.20 * 60 + max(0, short_return) / 0.20 * 30)
            components["decline_speed"] = {
                "score": round(risk_temperature, 6),
                "raw": round(short_return, 8),
                "explanation": f"近约 20 日变化 {short_return:.1%}；快速下跌会提高风险温度",
            }
        else:
            warnings.append("近 20 日价格不足")
        price_age_days = (cutoff - _time(current["as_of"])).total_seconds() / 86400
    else:
        current = None
        price_age_days = float("inf")
        warnings.append("没有历史时点可用价格")

    if metric:
        valuation = metric.get("valuation_percentile")
        if isinstance(valuation, (int, float)):
            components["valuation"] = {
                "score": round(_clamp(float(valuation) * 100), 6),
                "raw": round(float(valuation), 8),
                "explanation": f"估值处于自身历史 {float(valuation):.1%} 分位",
            }
        volatility = metric.get("volatility_annualized")
        if isinstance(volatility, (int, float)):
            components["volatility"] = {
                "score": round(_linear(float(volatility), 0.10, 0.40), 6),
                "raw": round(float(volatility), 8),
                "explanation": f"年化波动率 {float(volatility):.1%}",
            }
        current_weight = metric.get("portfolio_weight")
        target_weight = metric.get("target_weight")
        if isinstance(current_weight, (int, float)) and isinstance(target_weight, (int, float)):
            allocation_score = _clamp(50 + (float(current_weight) - float(target_weight)) / 0.20 * 50)
            components["allocation"] = {
                "score": round(allocation_score, 6),
                "raw": round(float(current_weight) - float(target_weight), 8),
                "explanation": f"当前组合权重与目标相差 {float(current_weight) - float(target_weight):.1%}",
            }
        metric_age_days = (cutoff - _time(metric["published_at"])).total_seconds() / 86400
    else:
        metric_age_days = float("inf")
        warnings.append("没有历史时点可用估值、波动或配置数据")

    weights = {
        "range_1y": 0.08,
        "range_3y": 0.06,
        "range_5y": 0.06,
        "drawdown": 0.15,
        "valuation": 0.20,
        "trend_6m": 0.075,
        "trend_12m": 0.075,
        "volatility": 0.10,
        "decline_speed": 0.10,
        "allocation": 0.10,
    }
    used_weight = sum(weights[key] for key in components if key in weights)
    total = round(sum(components[key]["score"] * weights[key] for key in components if key in weights) / used_weight, 6) if used_weight else None
    component_count = len([key for key in components if key in weights])
    if price_age_days > 7 or metric_age_days > 120 or component_count < 5 or not source_complete:
        confidence = "stopped"
        eligible = False
    elif component_count == len(weights) and metric_age_days <= 45:
        confidence = "high"
        eligible = True
    elif component_count >= 7:
        confidence = "medium"
        eligible = True
    else:
        confidence = "low"
        eligible = True
    return {
        "instrument_id": dataset.get("instrument_id"),
        "as_of": as_of,
        "scoring_version": INDEX_SCORING_VERSION,
        "temperature": total,
        "scale": {"minimum": 0, "maximum": 100, "meaning": "0=冷，100=热；高温不等于预测下跌"},
        "components": components,
        "weights": weights,
        "confidence": confidence,
        "eligible": eligible,
        "data_quality": {
            "component_count": component_count,
            "required_component_count": len(weights),
            "price_age_days": None if price_age_days == float("inf") else round(price_age_days, 3),
            "metric_age_days": None if metric_age_days == float("inf") else round(metric_age_days, 3),
            "future_records_excluded": future_prices + future_metrics,
            "sources": sorted(used_sources),
            "source_complete": source_complete,
            "warnings": warnings,
        },
    }


def score_watchlist_stock(dataset: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    cutoff = _time(as_of)
    financial, future_financials = _latest_available(dataset.get("financials", []), "published_at", cutoff)
    market, future_markets = _latest_available(dataset.get("market", []), "published_at", cutoff)
    red_lines = [
        item for item in dataset.get("red_flags", [])
        if _time(item["published_at"]) <= cutoff and item.get("active") and item.get("type") in RED_LINE_TYPES
    ]
    future_red_flags = len(dataset.get("red_flags", [])) - len([
        item for item in dataset.get("red_flags", []) if _time(item["published_at"]) <= cutoff
    ])
    components: dict[str, dict[str, Any]] = {}
    used_records = [record for record in (financial, market) if record is not None] + red_lines
    used_sources = {record.get("source") for record in used_records if record.get("source")}
    source_complete = bool(financial and market) and all(record.get("source") for record in used_records)

    def add(name: str, raw: Any, score: float, explanation: str) -> None:
        if raw is not None:
            components[name] = {"raw": raw, "score": round(_clamp(score), 6), "explanation": explanation}

    if financial:
        revenue = financial.get("revenue_growth")
        profit = financial.get("profit_growth")
        ocf = financial.get("operating_cash_flow_positive")
        fcf = financial.get("free_cash_flow_positive")
        roe = financial.get("roe")
        debt = financial.get("debt_ratio")
        coverage = financial.get("interest_coverage")
        if isinstance(revenue, (int, float)): add("revenue_growth", revenue, _linear(float(revenue), -0.20, 0.20), f"收入同比 {revenue:.1%}")
        if isinstance(profit, (int, float)): add("profit_growth", profit, _linear(float(profit), -0.30, 0.30), f"利润同比 {profit:.1%}")
        if isinstance(ocf, bool): add("operating_cash_flow", ocf, 100 if ocf else 0, "经营现金流为正" if ocf else "经营现金流为负")
        if isinstance(fcf, bool): add("free_cash_flow", fcf, 100 if fcf else 0, "自由现金流为正" if fcf else "自由现金流为负")
        if isinstance(roe, (int, float)): add("roe", roe, _linear(float(roe), 0, 0.20), f"ROE {roe:.1%}")
        if isinstance(debt, (int, float)): add("debt", debt, 100 - _linear(float(debt), 0.20, 0.80), f"资产负债率 {debt:.1%}")
        if isinstance(coverage, (int, float)): add("interest_coverage", coverage, _linear(float(coverage), 0, 10), f"利息保障倍数 {coverage:.1f}")
        financial_age = (cutoff - _time(financial["published_at"])).total_seconds() / 86400
    else:
        financial_age = float("inf")
    if market:
        valuation = market.get("valuation_percentile")
        trend = market.get("return_6m")
        volatility = market.get("volatility_annualized")
        if isinstance(valuation, (int, float)): add("valuation", valuation, 100 - float(valuation) * 100, f"估值历史分位 {valuation:.1%}")
        if isinstance(trend, (int, float)): add("trend", trend, _linear(float(trend), -0.30, 0.30), f"六个月价格变化 {trend:.1%}")
        if isinstance(volatility, (int, float)): add("volatility", volatility, 100 - _linear(float(volatility), 0.10, 0.60), f"年化波动率 {volatility:.1%}")
        market_age = (cutoff - _time(market["published_at"])).total_seconds() / 86400
    else:
        market_age = float("inf")

    weights = {
        "revenue_growth": 0.12, "profit_growth": 0.14, "operating_cash_flow": 0.12,
        "free_cash_flow": 0.10, "roe": 0.10, "debt": 0.10, "interest_coverage": 0.07,
        "valuation": 0.10, "trend": 0.08, "volatility": 0.07,
    }
    used_weight = sum(weights[key] for key in components)
    total = round(sum(components[key]["score"] * weights[key] for key in components) / used_weight, 6) if used_weight else None
    count = len(components)
    if red_lines or financial_age > 200 or market_age > 30 or count < 6 or not source_complete:
        confidence = "stopped"
        eligible = False
    elif count == len(weights) and financial_age <= 120 and market_age <= 7:
        confidence = "high"
        eligible = True
    elif count >= 8:
        confidence = "medium"
        eligible = True
    else:
        confidence = "low"
        eligible = True
    return {
        "instrument_id": dataset.get("instrument_id"),
        "as_of": as_of,
        "scoring_version": STOCK_SCORING_VERSION,
        "quality_score": total,
        "components": components,
        "weights": weights,
        "red_lines": red_lines,
        "confidence": confidence,
        "eligible": eligible,
        "data_quality": {
            "component_count": count,
            "required_component_count": len(weights),
            "financial_age_days": None if financial_age == float("inf") else round(financial_age, 3),
            "market_age_days": None if market_age == float("inf") else round(market_age, 3),
            "future_records_excluded": future_financials + future_markets + future_red_flags,
            "sources": sorted(used_sources),
            "source_complete": source_complete,
        },
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


class ScoreRepository:
    def __init__(self, data_dir: Path | str | None = None):
        directory = Path(data_dir) if data_dir is not None else default_data_dir()
        self.path = directory / "scores.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCORE_STORE_SCHEMA_VERSION, "revision": 0, "records": []}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("schema_version") != SCORE_STORE_SCHEMA_VERSION:
            raise ValueError("不支持的评分记录版本")
        return value

    def record(self, score: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        payload = self.load()
        if expected_revision is not None and expected_revision != payload["revision"]:
            raise RevisionConflictError(f"评分记录版本冲突：期望 {expected_revision}，当前 {payload['revision']}")
        payload["revision"] += 1
        payload["records"].append(score)
        _atomic_write(self.path, payload)
        return payload
