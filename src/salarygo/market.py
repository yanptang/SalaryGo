"""M3 provider interface, manual refresh flow and durable market-data cache."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .storage import default_data_dir

MARKET_CACHE_SCHEMA_VERSION = 1
DATA_KINDS = {"stock", "fund", "index", "nav", "fx"}
DEFAULT_MAX_AGE_SECONDS = {
    "stock": 36 * 3600,
    "fund": 72 * 3600,
    "index": 36 * 3600,
    "nav": 72 * 3600,
    "fx": 36 * 3600,
}


@dataclass(frozen=True)
class MarketRequest:
    key: str
    kind: str
    symbol: str


@dataclass(frozen=True)
class MarketQuote:
    key: str
    kind: str
    symbol: str
    value: float
    currency: str
    as_of: str
    source: str


class MarketDataProvider(Protocol):
    name: str

    def fetch(self, request: MarketRequest) -> MarketQuote:
        ...


class StaticMarketDataProvider:
    """Deterministic provider used by tests and offline acceptance fixtures."""

    def __init__(self, records: dict[str, dict[str, Any]], name: str = "fixture"):
        self.records = records
        self.name = name

    @classmethod
    def from_file(cls, path: Path | str) -> "StaticMarketDataProvider":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload["quotes"], payload.get("source", "fixture"))

    def fetch(self, request: MarketRequest) -> MarketQuote:
        if request.kind not in DATA_KINDS:
            raise ValueError(f"不支持的数据类型：{request.kind}")
        if request.key not in self.records:
            raise LookupError(f"数据源 {self.name} 没有 {request.key}")
        record = self.records[request.key]
        if record.get("error"):
            raise RuntimeError(str(record["error"]))
        return MarketQuote(
            key=request.key,
            kind=request.kind,
            symbol=request.symbol,
            value=float(record["value"]),
            currency=record["currency"],
            as_of=record["as_of"],
            source=self.name,
        )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


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


class MarketCacheRepository:
    def __init__(self, data_dir: Path | str | None = None):
        directory = Path(data_dir) if data_dir is not None else default_data_dir()
        self.path = directory / "market_cache.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": MARKET_CACHE_SCHEMA_VERSION, "quotes": {}, "attempts": []}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != MARKET_CACHE_SCHEMA_VERSION:
            raise ValueError("不支持的市场缓存版本")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        _atomic_write(self.path, payload)

    def snapshot(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        payload = self.load()
        checked_at = now or datetime.now(timezone.utc)
        limits = {**DEFAULT_MAX_AGE_SECONDS, **(max_age_seconds or {})}
        quotes: dict[str, Any] = {}
        warnings: list[dict[str, str]] = []
        for key, quote in payload["quotes"].items():
            age = max(0.0, (checked_at - _parse_time(quote["as_of"])).total_seconds())
            stale = age > limits[quote["kind"]]
            enriched = {**quote, "age_seconds": round(age, 3), "status": "stale" if stale else "fresh"}
            quotes[key] = enriched
            if stale:
                warnings.append({"key": key, "code": "stale", "message": f"{key} 数据已过期"})
        return {"checked_at": checked_at.isoformat(), "quotes": quotes, "warnings": warnings, "attempts": payload["attempts"]}


class MarketRefreshService:
    def __init__(self, repository: MarketCacheRepository):
        self.repository = repository

    def refresh(
        self,
        requests: list[MarketRequest],
        provider: MarketDataProvider,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        refreshed_at = now or datetime.now(timezone.utc)
        payload = self.repository.load()
        results: list[dict[str, Any]] = []
        for request in requests:
            attempt = {
                "key": request.key,
                "provider": provider.name,
                "attempted_at": refreshed_at.isoformat(),
            }
            try:
                quote = provider.fetch(request)
                if quote.key != request.key or quote.kind != request.kind or quote.symbol != request.symbol:
                    raise ValueError("数据源返回的标识与请求不一致")
                _parse_time(quote.as_of)
                if quote.value < 0:
                    raise ValueError("行情数值不能为负")
                stored = {**asdict(quote), "fetched_at": refreshed_at.isoformat()}
                payload["quotes"][request.key] = stored
                attempt.update({"status": "success", "source": quote.source, "as_of": quote.as_of})
                results.append({"key": request.key, "status": "success", "quote": stored})
            except Exception as exc:  # provider failures are isolated per request
                attempt.update({"status": "failed", "error": str(exc)})
                previous = payload["quotes"].get(request.key)
                results.append({
                    "key": request.key,
                    "status": "failed",
                    "error": str(exc),
                    "previous_preserved": previous is not None,
                    "quote": previous,
                })
            payload["attempts"].append(attempt)
        payload["attempts"] = payload["attempts"][-500:]
        self.repository.save(payload)
        return {"refreshed_at": refreshed_at.isoformat(), "provider": provider.name, "results": results}


def apply_quotes_to_ledger(ledger: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply available prices without clearing holdings on failed/missing refreshes."""
    updated = deepcopy(ledger)
    quotes = snapshot.get("quotes", {})
    for holding in updated.get("holdings", []):
        quote = quotes.get(holding["instrument_id"])
        if quote is None or quote.get("kind") == "fx":
            continue
        holding["current_price"] = quote["value"]
        holding["price_currency"] = quote["currency"]
        holding["price_as_of"] = quote["as_of"]
        holding["data_source"] = quote["source"]
    return updated

