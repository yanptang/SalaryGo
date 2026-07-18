"""M4 deterministic candidate-product filtering, ranking and pool history."""

from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import RevisionConflictError, default_data_dir

CANDIDATE_SCHEMA_VERSION = 1
SCORING_VERSION = "candidate-quality-v1"
REVIEW_REASONS = {"initial", "scheduled_quarterly", "product_change", "availability_event", "risk_event"}


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _valid_number(value: Any, *, minimum: float = 0.0) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum


def hard_exclusions(slot: dict[str, Any], product: dict[str, Any], planned_amount: float) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    if product.get("exposure") != slot.get("exposure"):
        reasons.append({"code": "wrong_exposure", "message": "跟踪目标与席位要求不一致"})
    if product.get("purchasable") is not True:
        reasons.append({"code": "not_purchasable", "message": "当前不可购买"})
    limit = product.get("subscription_limit")
    if not _valid_number(limit) or limit < planned_amount:
        reasons.append({"code": "insufficient_limit", "message": "申购限额不足"})
    if product.get("leveraged") or product.get("inverse") or product.get("complex_structure"):
        reasons.append({"code": "complex_product", "message": "杠杆、反向或结构复杂"})
    numeric_required = ("tracking_error", "annual_cost", "aum", "liquidity_score", "age_years")
    missing = [field for field in numeric_required if product.get(field) is None]
    if not isinstance(product.get("status_as_of"), str) or not product["status_as_of"].strip():
        missing.append("status_as_of")
    if missing:
        reasons.append({"code": "missing_data", "message": f"关键数据缺失：{', '.join(missing)}"})
    invalid = [field for field in numeric_required if product.get(field) is not None and not _valid_number(product[field])]
    if invalid:
        reasons.append({"code": "invalid_data", "message": f"关键数值非法：{', '.join(invalid)}"})
    if _valid_number(product.get("aum")) and product["aum"] < slot.get("minimum_aum", 0):
        reasons.append({"code": "insufficient_scale", "message": "产品规模低于席位下限"})
    if _valid_number(product.get("liquidity_score")) and product["liquidity_score"] < slot.get("minimum_liquidity_score", 0):
        reasons.append({"code": "insufficient_liquidity", "message": "流动性低于席位下限"})
    if product.get("is_qdii"):
        premium = product.get("premium")
        if not _valid_number(premium):
            reasons.append({"code": "missing_premium", "message": "QDII 折溢价数据缺失"})
        elif premium > slot.get("maximum_qdii_premium", 0.03):
            reasons.append({"code": "high_premium", "message": "QDII 溢价超过席位阈值"})
    return reasons


def score_product(product: dict[str, Any], planned_amount: float) -> dict[str, Any]:
    """Score implementation quality only; recent investment return is excluded."""
    tracking = _clamp(100 - float(product["tracking_error"]) / 0.02 * 100)
    cost = _clamp(100 - float(product["annual_cost"]) / 0.02 * 100)
    scale = _clamp(math.log10(max(float(product["aum"]), 1)) / 4 * 100)
    liquidity = _clamp(float(product["liquidity_score"]))
    history = _clamp(float(product["age_years"]) / 5 * 100)
    limit = float(product["subscription_limit"])
    operability = _clamp(70 + min(limit / max(planned_amount, 1), 3) * 10 - float(product.get("operation_steps", 1)) * 5)
    components = {
        "tracking": round(tracking, 6),
        "cost": round(cost, 6),
        "scale": round(scale, 6),
        "liquidity": round(liquidity, 6),
        "history": round(history, 6),
        "operability": round(operability, 6),
    }
    weights = {"tracking": 0.30, "cost": 0.25, "scale": 0.15, "liquidity": 0.15, "history": 0.05, "operability": 0.10}
    total = round(sum(components[key] * weights[key] for key in components), 6)
    return {"total": total, "components": components, "scoring_version": SCORING_VERSION}


def select_products(
    slots: list[dict[str, Any]],
    products: list[dict[str, Any]],
    *,
    planned_amounts: dict[str, float],
    as_of: str,
) -> dict[str, Any]:
    assignments: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for slot in sorted(slots, key=lambda item: item["id"]):
        planned = planned_amounts.get(slot["id"], 0)
        eligible: list[dict[str, Any]] = []
        for product in sorted(products, key=lambda item: item["id"]):
            reasons = hard_exclusions(slot, product, planned)
            if reasons:
                if product.get("exposure") == slot.get("exposure"):
                    exclusions.append({"slot_id": slot["id"], "product_id": product["id"], "reasons": reasons})
                continue
            score = score_product(product, planned)
            eligible.append({"product_id": product["id"], "score": score})
        eligible.sort(key=lambda item: (-item["score"]["total"], item["product_id"]))
        main = eligible[0] if eligible else None
        backup = eligible[1] if len(eligible) > 1 else None
        assignment = {
            "slot_id": slot["id"],
            "main": main,
            "backup": backup,
            "eligible_ranking": eligible,
            "as_of": as_of,
            "selection_reason": (
                f"通过硬性条件后实施质量得分最高（{main['score']['total']:.2f}），"
                "评分基于跟踪、成本、规模、流动性、历史和操作复杂度"
                if main else "没有产品通过硬性条件，席位暂不分配主产品"
            ),
        }
        assignments.append(assignment)
    return {
        "scoring_version": SCORING_VERSION,
        "as_of": as_of,
        "assignments": assignments,
        "exclusions": exclusions,
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


class CandidatePoolRepository:
    def __init__(self, data_dir: Path | str | None = None):
        directory = Path(data_dir) if data_dir is not None else default_data_dir()
        self.path = directory / "candidate_pool.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "revision": 0,
                "pool_version": 0,
                "current": None,
                "history": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
            raise ValueError("不支持的候选池版本")
        return value

    def record_review(
        self,
        selection: dict[str, Any],
        *,
        reason: str,
        expected_revision: int | None = None,
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        if reason not in REVIEW_REASONS:
            raise ValueError(f"不支持的复核原因：{reason}")
        payload = self.load()
        if expected_revision is not None and payload["revision"] != expected_revision:
            raise RevisionConflictError(f"候选池版本冲突：期望 {expected_revision}，当前 {payload['revision']}")
        previous = payload["current"]
        changed = previous != selection
        timestamp = (recorded_at or datetime.now(timezone.utc)).isoformat()
        payload["revision"] += 1
        if changed:
            payload["pool_version"] += 1
        payload["current"] = deepcopy(selection)
        payload["history"].append({
            "event_id": f"review-{payload['revision']}",
            "recorded_at": timestamp,
            "reason": reason,
            "changed": changed,
            "pool_version": payload["pool_version"],
            "previous": previous,
            "current": deepcopy(selection),
        })
        _atomic_write(self.path, payload)
        return payload
