"""M7 deterministic shell around Codex conversation and strategy records."""

from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .allocation import generate_allocation
from .storage import default_data_dir

STRATEGY_SCHEMA_VERSION = 1
EXECUTION_STATUSES = {"proposed", "planned", "partially_executed", "executed", "rejected"}

CURRENCY_ALIASES = {
    "人民币": "CNY", "元": "CNY", "CNY": "CNY", "¥": "CNY", "￥": "CNY",
    "瑞典克朗": "SEK", "克朗": "SEK", "SEK": "SEK",
    "美元": "USD", "USD": "USD", "$": "USD",
}


def extract_funds(text: str) -> dict[str, Any]:
    """Extract explicit amount/currency pairs; ambiguous values stay unresolved."""
    pattern = re.compile(
        r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>万|千|k|K)?\s*"
        r"(?P<currency>人民币|瑞典克朗|克朗|美元|CNY|SEK|USD|元|¥|￥|\$)?"
    )
    funds: list[dict[str, Any]] = []
    unresolved: list[float] = []
    for match in pattern.finditer(text):
        raw = float(match.group("amount"))
        unit = match.group("unit")
        multiplier = 10000 if unit == "万" else 1000 if unit in {"千", "k", "K"} else 1
        amount = raw * multiplier
        currency_text = match.group("currency")
        if currency_text:
            funds.append({"amount": round(amount, 2), "currency": CURRENCY_ALIASES[currency_text]})
        else:
            unresolved.append(round(amount, 2))
    # Merge repeated currency mentions so downstream money conservation is simple.
    merged: dict[str, float] = {}
    for fund in funds:
        merged[fund["currency"]] = round(merged.get(fund["currency"], 0) + fund["amount"], 2)
    result = [{"amount": amount, "currency": currency} for currency, amount in merged.items()]
    missing: list[str] = []
    questions: list[str] = []
    if not result and not unresolved:
        missing.append("amount")
        questions.append("这次新增可投资资金是多少？请同时说明币种。")
    if unresolved:
        missing.append("currency")
        questions.append(f"金额 {', '.join(str(value) for value in unresolved)} 的币种是什么？")
    return {"funds": result, "unresolved_amounts": unresolved, "missing": missing, "questions": questions}


ONBOARDING_FIELDS = [
    ("investment_plan.horizon_years", "计划投资多长时间？"),
    ("investment_plan.target_annual_return", "目标年化收益率是多少？这只是目标，不是保证。"),
    ("investment_plan.max_drawdown", "最大可以接受多大阶段性回撤？"),
    ("investment_plan.monthly_investment", "每月通常可投资多少、使用什么币种？"),
    ("emergency_fund", "应急资金可覆盖多少个月日常开支？"),
    ("accounts", "目前可使用哪些账户和市场？"),
    ("concentration_limits", "单只个股、个股合计、市场、行业和币种上限分别是多少？"),
    ("exclusions", "有哪些明确不投资的资产、行业、公司或产品？"),
]


def onboarding_questions(draft: dict[str, Any]) -> list[dict[str, str]]:
    def present(path: str) -> bool:
        value: Any = draft
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        return value is not None

    return [{"field": path, "question": question} for path, question in ONBOARDING_FIELDS if not present(path)]


def render_strategy_markdown(record: dict[str, Any]) -> str:
    plan = record["plan"]
    lines = [
        f"# SalaryGo 月度策略 {record['strategy_id']}", "",
        f"生成时间：{record['generated_at']}", "",
        "> 本报告仅为个人投资决策辅助，不构成收益承诺；所有交易均需用户自行确认和执行。", "",
        "## 本次资金", "",
    ]
    for fund in plan["input_funds"]:
        lines.append(f"- {fund['currency']} {fund['amount']:,.2f}")
    lines.extend(["", "## 建议", ""])
    for index, item in enumerate(plan["recommendations"], 1):
        operation = "暂留现金" if item["operation"] == "hold_cash" else "买入"
        lines.extend([
            f"### {index}. {operation}：{item['name']}", "",
            f"- 平台：{item['platform']}",
            f"- 金额：{item['funding_currency']} {item['amount']:,.2f}",
            f"- 资金桶：{item['bucket']}",
            f"- 依据代码：{', '.join(item['reason_codes'])}",
            f"- 水温：{item['temperature'] if item['temperature'] is not None else '不适用'}",
            f"- 数据置信度：{item['confidence']}", "",
        ])
    lines.extend([
        "## 风险与失效条件", "",
        "- 权益和个股可能出现显著回撤，目标收益率不保证实现。",
        "- 海外资产会受到汇率、费用、交易时间和账户可购买状态影响。",
        "- 行情、净值、估值、产品状态或个人资金用途发生变化时应重新生成策略。",
        "- 普通水温变化默认不构成卖出理由。", "",
        f"规则版本：{plan['allocation_rule_version']} / {plan['risk_rule_version']}", "",
    ])
    return "\n".join(lines)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


class StrategyRepository:
    def __init__(self, data_dir: Path | str | None = None):
        private = Path(data_dir) if data_dir is not None else default_data_dir()
        self.directory = private / "strategies"
        self.report_directory = private.parent / "reports"

    def save_plan(self, plan: dict[str, Any], *, user_request: str, explanation: str | None = None) -> dict[str, Any]:
        if plan.get("status") != "formal" or not plan.get("risk_validation", {}).get("passed"):
            raise ValueError("只有通过确定性风控的正式方案可以保存为策略")
        now = datetime.now(timezone.utc).isoformat()
        strategy_id = f"strategy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        record = {
            "schema_version": STRATEGY_SCHEMA_VERSION,
            "strategy_id": strategy_id,
            "generated_at": now,
            "user_request": user_request,
            "plan": deepcopy(plan),
            "agent_explanation": explanation or "解释严格依据结构化计划生成，金额与风控结果未被修改。",
            "execution_status": "proposed",
            "actual_trades": [],
            "updated_at": now,
        }
        _atomic_text(self.directory / f"{strategy_id}.json", json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_text(self.report_directory / f"{strategy_id}.md", render_strategy_markdown(record))
        return record

    def load(self, strategy_id: str) -> dict[str, Any]:
        path = self.directory / f"{strategy_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"策略不存在：{strategy_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.directory.glob("*.json"), reverse=True)]

    def update_execution(self, strategy_id: str, status: str, actual_trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if status not in EXECUTION_STATUSES:
            raise ValueError(f"不支持的执行状态：{status}")
        record = self.load(strategy_id)
        record["execution_status"] = status
        if actual_trades is not None:
            record["actual_trades"] = deepcopy(actual_trades)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_text(self.directory / f"{strategy_id}.json", json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_text(self.report_directory / f"{strategy_id}.md", render_strategy_markdown(record))
        return record


class AgentWorkflow:
    def __init__(self, strategies: StrategyRepository):
        self.strategies = strategies

    def monthly_request(self, text: str, allocation_context: dict[str, Any]) -> dict[str, Any]:
        extracted = extract_funds(text)
        if extracted["missing"]:
            return {"status": "needs_input", **extracted}
        context = deepcopy(allocation_context)
        context["new_funds"] = extracted["funds"]
        plan = generate_allocation(context)
        if plan["status"] != "formal":
            return {"status": "blocked", "plan": plan, "message": "确定性风控未通过，未生成正式建议。"}
        record = self.strategies.save_plan(plan, user_request=text)
        return {"status": "formal", "strategy": record, "report": render_strategy_markdown(record)}

