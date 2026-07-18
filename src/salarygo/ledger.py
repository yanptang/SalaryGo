"""M2 holding ledger, CRUD operations and deterministic portfolio valuation."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .profile import SUPPORTED_CURRENCIES, SUPPORTED_MARKETS, ValidationIssue
from .storage import RevisionConflictError, default_data_dir

LEDGER_SCHEMA_VERSION = 1
ACCOUNT_PLATFORMS = {"EASTMONEY", "REVOLUT"}
ASSET_TYPES = {"cash_fund", "bond_fund", "index_fund", "etf", "stock"}
BUCKETS = {"cash", "defensive_bond", "index_core", "individual_stock"}
TRANSACTION_TYPES = {"buy", "sell", "dividend", "fee", "transfer_in", "transfer_out"}


class LedgerValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


class EntityNotFoundError(KeyError):
    pass


def _number(value: Any, *, positive: bool = False, nonnegative: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if positive:
        return value > 0
    if nonnegative:
        return value >= 0
    return True


def _required_string(record: dict[str, Any], key: str, path: str, issues: list[ValidationIssue]) -> None:
    if not isinstance(record.get(key), str) or not record[key].strip():
        issues.append(ValidationIssue(f"{path}.{key}", "required", "必须是非空字符串"))


def validate_ledger(ledger: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(ledger, dict):
        return [ValidationIssue("$", "type", "账本必须是对象")]
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        issues.append(ValidationIssue("schema_version", "unsupported_schema", "当前仅支持账本版本 1"))
    _required_string(ledger, "ledger_id", "$", issues)
    revision = ledger.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        issues.append(ValidationIssue("revision", "range", "必须是大于等于 0 的整数"))
    if ledger.get("base_currency") not in SUPPORTED_CURRENCIES:
        issues.append(ValidationIssue("base_currency", "enum", "不支持的基准币种"))

    accounts = ledger.get("accounts")
    instruments = ledger.get("instruments")
    holdings = ledger.get("holdings")
    transactions = ledger.get("transactions")
    for key, value in (("accounts", accounts), ("instruments", instruments), ("holdings", holdings), ("transactions", transactions)):
        if not isinstance(value, list):
            issues.append(ValidationIssue(key, "type", "必须是数组"))
    if issues and not all(isinstance(value, list) for value in (accounts, instruments, holdings, transactions)):
        return issues

    account_ids: set[str] = set()
    for index, account in enumerate(accounts):
        path = f"accounts[{index}]"
        if not isinstance(account, dict):
            issues.append(ValidationIssue(path, "type", "必须是对象"))
            continue
        _required_string(account, "id", path, issues)
        _required_string(account, "name", path, issues)
        account_id = account.get("id")
        if account_id in account_ids:
            issues.append(ValidationIssue(f"{path}.id", "duplicate", "账户 ID 重复"))
        if isinstance(account_id, str):
            account_ids.add(account_id)
        if account.get("platform") not in ACCOUNT_PLATFORMS:
            issues.append(ValidationIssue(f"{path}.platform", "enum", "仅支持 EASTMONEY 或 REVOLUT"))
        if account.get("base_currency") not in SUPPORTED_CURRENCIES:
            issues.append(ValidationIssue(f"{path}.base_currency", "enum", "不支持的账户币种"))

    instrument_ids: set[str] = set()
    for index, instrument in enumerate(instruments):
        path = f"instruments[{index}]"
        if not isinstance(instrument, dict):
            issues.append(ValidationIssue(path, "type", "必须是对象"))
            continue
        for key in ("id", "symbol", "name"):
            _required_string(instrument, key, path, issues)
        instrument_id = instrument.get("id")
        if instrument_id in instrument_ids:
            issues.append(ValidationIssue(f"{path}.id", "duplicate", "标的 ID 重复"))
        if isinstance(instrument_id, str):
            instrument_ids.add(instrument_id)
        if instrument.get("market") not in SUPPORTED_MARKETS:
            issues.append(ValidationIssue(f"{path}.market", "enum", "不支持的市场"))
        if instrument.get("asset_type") not in ASSET_TYPES:
            issues.append(ValidationIssue(f"{path}.asset_type", "enum", "不支持的资产类型"))
        if instrument.get("bucket") not in BUCKETS:
            issues.append(ValidationIssue(f"{path}.bucket", "enum", "不支持的资金桶"))
        if instrument.get("currency") not in SUPPORTED_CURRENCIES:
            issues.append(ValidationIssue(f"{path}.currency", "enum", "不支持的标的币种"))

    holding_ids: set[str] = set()
    pairs: set[tuple[Any, Any]] = set()
    for index, holding in enumerate(holdings):
        path = f"holdings[{index}]"
        if not isinstance(holding, dict):
            issues.append(ValidationIssue(path, "type", "必须是对象"))
            continue
        _required_string(holding, "id", path, issues)
        holding_id = holding.get("id")
        if holding_id in holding_ids:
            issues.append(ValidationIssue(f"{path}.id", "duplicate", "持仓 ID 重复"))
        if isinstance(holding_id, str):
            holding_ids.add(holding_id)
        account_id = holding.get("account_id")
        instrument_id = holding.get("instrument_id")
        if account_id not in account_ids:
            issues.append(ValidationIssue(f"{path}.account_id", "reference", "引用的账户不存在"))
        if instrument_id not in instrument_ids:
            issues.append(ValidationIssue(f"{path}.instrument_id", "reference", "引用的标的不存在"))
        pair = (account_id, instrument_id)
        if pair in pairs:
            issues.append(ValidationIssue(path, "duplicate", "同一账户和标的只能有一条持仓"))
        pairs.add(pair)
        if not _number(holding.get("quantity"), nonnegative=True):
            issues.append(ValidationIssue(f"{path}.quantity", "range", "必须是非负数字"))
        if not _number(holding.get("average_cost"), nonnegative=True):
            issues.append(ValidationIssue(f"{path}.average_cost", "range", "必须是非负数字"))
        if holding.get("cost_currency") not in SUPPORTED_CURRENCIES:
            issues.append(ValidationIssue(f"{path}.cost_currency", "enum", "不支持的成本币种"))
        price = holding.get("current_price")
        if price is not None and not _number(price, nonnegative=True):
            issues.append(ValidationIssue(f"{path}.current_price", "range", "价格必须是 null 或非负数字"))
        if price is not None:
            if holding.get("price_currency") not in SUPPORTED_CURRENCIES:
                issues.append(ValidationIssue(f"{path}.price_currency", "enum", "有价格时必须提供价格币种"))
            _required_string(holding, "price_as_of", path, issues)

    transaction_ids: set[str] = set()
    for index, transaction in enumerate(transactions):
        path = f"transactions[{index}]"
        if not isinstance(transaction, dict):
            issues.append(ValidationIssue(path, "type", "必须是对象"))
            continue
        _required_string(transaction, "id", path, issues)
        transaction_id = transaction.get("id")
        if transaction_id in transaction_ids:
            issues.append(ValidationIssue(f"{path}.id", "duplicate", "交易 ID 重复"))
        if isinstance(transaction_id, str):
            transaction_ids.add(transaction_id)
        if transaction.get("account_id") not in account_ids:
            issues.append(ValidationIssue(f"{path}.account_id", "reference", "引用的账户不存在"))
        instrument_id = transaction.get("instrument_id")
        if instrument_id is not None and instrument_id not in instrument_ids:
            issues.append(ValidationIssue(f"{path}.instrument_id", "reference", "引用的标的不存在"))
        if transaction.get("type") not in TRANSACTION_TYPES:
            issues.append(ValidationIssue(f"{path}.type", "enum", "不支持的交易类型"))
        if transaction.get("type") in {"buy", "sell"}:
            if not _number(transaction.get("quantity"), positive=True):
                issues.append(ValidationIssue(f"{path}.quantity", "range", "买卖交易必须提供大于 0 的成交数量"))
            if not _number(transaction.get("unit_price"), nonnegative=True):
                issues.append(ValidationIssue(f"{path}.unit_price", "range", "买卖交易必须提供非负成交单价"))
        if not _number(transaction.get("amount"), positive=True):
            issues.append(ValidationIssue(f"{path}.amount", "range", "交易金额必须大于 0"))
        if transaction.get("currency") not in SUPPORTED_CURRENCIES:
            issues.append(ValidationIssue(f"{path}.currency", "enum", "不支持的交易币种"))
        if not _number(transaction.get("fee", 0), nonnegative=True):
            issues.append(ValidationIssue(f"{path}.fee", "range", "费用必须是非负数字"))
        _required_string(transaction, "executed_at", path, issues)
    return issues


def assert_valid_ledger(ledger: Any) -> None:
    issues = validate_ledger(ledger)
    if issues:
        raise LedgerValidationError(issues)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


class LedgerRepository:
    def __init__(self, data_dir: Path | str | None = None):
        self.path = Path(data_dir) / "ledger.json" if data_dir is not None else default_data_dir() / "ledger.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"持仓账本不存在：{self.path}")
        ledger = json.loads(self.path.read_text(encoding="utf-8"))
        assert_valid_ledger(ledger)
        return ledger

    def save(self, ledger: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        candidate = deepcopy(ledger)
        current = self.load() if self.path.exists() else None
        current_revision = current["revision"] if current else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise RevisionConflictError(f"账本版本冲突：期望 {expected_revision}，当前 {current_revision}")
        now = datetime.now(timezone.utc).isoformat()
        candidate["revision"] = current_revision + 1
        candidate["created_at"] = current["created_at"] if current else now
        candidate["updated_at"] = now
        assert_valid_ledger(candidate)
        _atomic_write(self.path, candidate)
        return candidate


class LedgerService:
    """Manual CRUD with validation delegated to the repository."""

    COLLECTIONS = {"account": "accounts", "instrument": "instruments", "holding": "holdings", "transaction": "transactions"}

    def __init__(self, repository: LedgerRepository):
        self.repository = repository

    def add(self, entity: str, record: dict[str, Any]) -> dict[str, Any]:
        ledger = self.repository.load()
        collection = self._collection(entity)
        ledger[collection].append(deepcopy(record))
        return self.repository.save(ledger, expected_revision=ledger["revision"])

    def update(self, entity: str, entity_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        ledger = self.repository.load()
        collection = self._collection(entity)
        for index, record in enumerate(ledger[collection]):
            if record.get("id") == entity_id:
                updated = {**record, **deepcopy(changes), "id": entity_id}
                ledger[collection][index] = updated
                return self.repository.save(ledger, expected_revision=ledger["revision"])
        raise EntityNotFoundError(f"未找到 {entity}：{entity_id}")

    def delete(self, entity: str, entity_id: str) -> dict[str, Any]:
        ledger = self.repository.load()
        collection = self._collection(entity)
        before = len(ledger[collection])
        ledger[collection] = [record for record in ledger[collection] if record.get("id") != entity_id]
        if len(ledger[collection]) == before:
            raise EntityNotFoundError(f"未找到 {entity}：{entity_id}")
        return self.repository.save(ledger, expected_revision=ledger["revision"])

    def _collection(self, entity: str) -> str:
        try:
            return self.COLLECTIONS[entity]
        except KeyError as exc:
            raise ValueError(f"不支持的实体类型：{entity}") from exc


def value_portfolio(ledger: dict[str, Any], fx_to_base: dict[str, float]) -> dict[str, Any]:
    """Calculate only values supported by both price and FX data."""
    assert_valid_ledger(ledger)
    base = ledger["base_currency"]
    rates = {**fx_to_base, base: 1.0}
    instruments = {item["id"]: item for item in ledger["instruments"]}
    accounts = {item["id"]: item for item in ledger["accounts"]}
    details: list[dict[str, Any]] = []
    known_total = 0.0
    groups: dict[str, dict[str, float]] = {key: {} for key in ("account", "platform", "market", "bucket", "currency", "instrument")}
    unknown_ids: list[str] = []

    for holding in ledger["holdings"]:
        instrument = instruments[holding["instrument_id"]]
        account = accounts[holding["account_id"]]
        price = holding.get("current_price")
        currency = holding.get("price_currency") or instrument["currency"]
        rate = rates.get(currency)
        missing: list[str] = []
        if price is None:
            missing.append("price")
        if rate is None:
            missing.append(f"fx:{currency}->{base}")
        market_value = None if missing else round(holding["quantity"] * price * rate, 8)
        cost_rate = rates.get(holding["cost_currency"])
        cost_value = None if cost_rate is None else round(holding["quantity"] * holding["average_cost"] * cost_rate, 8)
        profit = None if market_value is None or cost_value is None else round(market_value - cost_value, 8)
        if market_value is None:
            unknown_ids.append(holding["id"])
        else:
            known_total += market_value
            dimensions = {
                "account": account["id"],
                "platform": account["platform"],
                "market": instrument["market"],
                "bucket": instrument["bucket"],
                "currency": currency,
                "instrument": instrument["id"],
            }
            for dimension, key in dimensions.items():
                groups[dimension][key] = groups[dimension].get(key, 0.0) + market_value
        details.append({
            "holding_id": holding["id"],
            "account_id": account["id"],
            "platform": account["platform"],
            "instrument_id": instrument["id"],
            "market_value": market_value,
            "cost_value": cost_value,
            "unrealized_profit": profit,
            "base_currency": base,
            "missing": missing,
        })

    normalized_groups: dict[str, list[dict[str, Any]]] = {}
    for dimension, values in groups.items():
        normalized_groups[dimension] = [
            {
                "key": key,
                "market_value": round(value, 8),
                "known_value_share": round(value / known_total, 8) if known_total else None,
            }
            for key, value in sorted(values.items())
        ]
    return {
        "base_currency": base,
        "known_market_value": round(known_total, 8),
        "complete": not unknown_ids,
        "unknown_holding_ids": unknown_ids,
        "holdings": details,
        "concentration": normalized_groups,
    }
