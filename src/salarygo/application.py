"""Application service joining M1-M9 without exposing raw file edits."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent import AgentWorkflow, EXECUTION_STATUSES, StrategyRepository
from .allocation import generate_allocation
from .backtest import render_backtest_markdown, run_backtest
from .candidates import CandidatePoolRepository
from .ledger import LedgerRepository, LedgerService, value_portfolio
from .market import MarketCacheRepository, MarketRefreshService, MarketRequest, StaticMarketDataProvider, apply_quotes_to_ledger
from .profile import validate_profile
from .scoring import ScoreRepository
from .storage import ProfileRepository, default_data_dir


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


class SalaryGoApplication:
    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.profile = ProfileRepository(self.data_dir)
        self.ledger = LedgerRepository(self.data_dir)
        self.market = MarketCacheRepository(self.data_dir)
        self.candidates = CandidatePoolRepository(self.data_dir)
        self.scores = ScoreRepository(self.data_dir)
        self.strategies = StrategyRepository(self.data_dir)
        self.context_path = self.data_dir / "allocation_context.json"
        self.valuation_history_path = self.data_dir / "valuation_history.json"
        self.backtest_directory = self.data_dir.parent / "reports" / "backtests"

    @staticmethod
    def _optional(loader: Any) -> Any:
        try:
            return loader()
        except FileNotFoundError:
            return None

    def state(self) -> dict[str, Any]:
        profile = self._optional(self.profile.load)
        ledger = self._optional(self.ledger.load)
        market = self.market.snapshot()
        valuation = None
        if ledger:
            fx = {ledger["base_currency"]: 1.0}
            for key, quote in market["quotes"].items():
                if quote["kind"] == "fx" and key.endswith(f"_{ledger['base_currency']}"):
                    fx[key.split("_")[0]] = quote["value"]
            valuation = value_portfolio(ledger, fx)
        return {
            "profile": profile,
            "ledger": ledger,
            "valuation": valuation,
            "market": market,
            "candidate_pool": self.candidates.load(),
            "scores": self.scores.load(),
            "strategies": self.strategies.list(),
            "allocation_context": self.load_allocation_context(),
            "valuation_history": self.load_valuation_history(),
            "readiness": {
                "profile": profile is not None,
                "ledger": ledger is not None,
                "market": bool(market["quotes"]),
                "allocation_context": self.context_path.exists(),
            },
        }

    def save_profile(self, value: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        issues = validate_profile(value)
        # Revision/timestamps are storage-owned; a draft is still validated by ProfileRepository after injection.
        structural = [issue for issue in issues if issue.path not in {"revision", "created_at", "updated_at"}]
        if structural:
            from .profile import ProfileValidationError
            raise ProfileValidationError(structural)
        return self.profile.save(value, expected_revision=expected_revision)

    def save_ledger(self, value: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        saved = self.ledger.save(value, expected_revision=expected_revision)
        self.record_valuation_snapshot()
        return saved

    def mutate_ledger(self, entity: str, action: str, entity_id: str | None, record: dict[str, Any] | None) -> dict[str, Any]:
        service = LedgerService(self.ledger)
        if action == "add":
            result = service.add(entity, record or {})
        elif action == "update" and entity_id:
            result = service.update(entity, entity_id, record or {})
        elif action == "delete" and entity_id:
            result = service.delete(entity, entity_id)
        else:
            raise ValueError("无效的账本操作")
        self.record_valuation_snapshot()
        return result

    def refresh(self, requests: list[dict[str, str]], fixture_path: Path | str) -> dict[str, Any]:
        provider = StaticMarketDataProvider.from_file(fixture_path)
        result = MarketRefreshService(self.market).refresh([MarketRequest(**item) for item in requests], provider)
        ledger = self._optional(self.ledger.load)
        if ledger:
            updated = apply_quotes_to_ledger(ledger, self.market.snapshot())
            self.ledger.save(updated, expected_revision=ledger["revision"])
            self.record_valuation_snapshot()
        return result

    def import_market_data(self, source: str, quotes: dict[str, dict[str, Any]], requests: list[dict[str, str]]) -> dict[str, Any]:
        if not source.strip():
            raise ValueError("必须提供数据来源名称")
        provider = StaticMarketDataProvider(quotes, name=source)
        return MarketRefreshService(self.market).refresh([MarketRequest(**item) for item in requests], provider)

    def save_allocation_context(self, context: dict[str, Any]) -> dict[str, Any]:
        # A context is accepted only if the engine can produce either a safe formal plan or a clear block.
        result = generate_allocation(context)
        if result.get("errors") and any(error["code"] in {"missing_input", "invalid_input", "missing_fx"} for error in result["errors"]):
            raise ValueError(result["errors"])
        _atomic_json(self.context_path, context)
        return {"saved": True, "validation": result}

    def load_allocation_context(self) -> dict[str, Any] | None:
        if not self.context_path.exists():
            return None
        return json.loads(self.context_path.read_text(encoding="utf-8"))

    def generate_strategy(self, text: str) -> dict[str, Any]:
        context = self.load_allocation_context()
        if context is None:
            return {"status": "needs_input", "questions": ["请先在设置中完成候选产品和目标配置。"]}
        return AgentWorkflow(self.strategies).monthly_request(text, context)

    def record_execution(self, strategy_id: str, status: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
        # Updating actual holdings is a separate, explicitly confirmed action.
        self.strategies.load(strategy_id)
        if status not in EXECUTION_STATUSES:
            raise ValueError(f"不支持的执行状态：{status}")
        ledger = self.ledger.load()
        updated = deepcopy(ledger)
        for trade in trades:
            holding = next((item for item in updated["holdings"] if item["id"] == trade["holding_id"]), None)
            if holding is None:
                raise ValueError(f"持仓不存在：{trade['holding_id']}")
            quantity = float(trade["quantity"])
            unit_price = float(trade["unit_price"])
            if quantity <= 0 or unit_price < 0:
                raise ValueError("成交数量必须大于 0，价格不能为负")
            old_quantity = float(holding["quantity"])
            if trade["type"] == "buy":
                new_quantity = old_quantity + quantity
                holding["average_cost"] = (old_quantity * float(holding["average_cost"]) + quantity * unit_price) / new_quantity
                holding["quantity"] = new_quantity
            elif trade["type"] == "sell":
                if quantity > old_quantity:
                    raise ValueError("卖出数量超过当前持仓")
                holding["quantity"] = old_quantity - quantity
            else:
                raise ValueError("实际成交仅支持 buy 或 sell")
            updated["transactions"].append({
                "id": f"trade-{uuid4().hex[:12]}", "account_id": holding["account_id"],
                "instrument_id": holding["instrument_id"], "type": trade["type"], "quantity": quantity,
                "unit_price": unit_price, "amount": float(trade.get("amount", quantity * unit_price)),
                "currency": trade["currency"], "fee": float(trade.get("fee", 0)),
                "executed_at": trade["executed_at"], "notes": f"来自策略 {strategy_id}",
            })
        saved_ledger = self.ledger.save(updated, expected_revision=ledger["revision"])
        saved_strategy = self.strategies.update_execution(strategy_id, status, trades)
        self.record_valuation_snapshot()
        return {"ledger": saved_ledger, "strategy": saved_strategy}

    def load_valuation_history(self) -> list[dict[str, Any]]:
        if not self.valuation_history_path.exists():
            return []
        return json.loads(self.valuation_history_path.read_text(encoding="utf-8"))["snapshots"]

    def record_valuation_snapshot(self) -> None:
        ledger = self._optional(self.ledger.load)
        if ledger is None:
            return
        snapshot = self.market.snapshot()
        fx = {ledger["base_currency"]: 1.0}
        for key, quote in snapshot["quotes"].items():
            if quote["kind"] == "fx" and key.endswith(f"_{ledger['base_currency']}"):
                fx[key.split("_")[0]] = quote["value"]
        valuation = value_portfolio(ledger, fx)
        payload = {"schema_version": 1, "snapshots": self.load_valuation_history()}
        payload["snapshots"].append({
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "known_market_value": valuation["known_market_value"],
            "base_currency": valuation["base_currency"],
            "complete": valuation["complete"],
        })
        payload["snapshots"] = payload["snapshots"][-500:]
        _atomic_json(self.valuation_history_path, payload)

    def run_backtest(self, config: dict[str, Any]) -> dict[str, Any]:
        report = run_backtest(config)
        self.backtest_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        _atomic_json(self.backtest_directory / f"backtest-{stamp}.json", report)
        markdown = render_backtest_markdown(report)
        path = self.backtest_directory / f"backtest-{stamp}.md"
        path.write_text(markdown, encoding="utf-8")
        path.chmod(0o600)
        return {"report": report, "markdown": markdown}
