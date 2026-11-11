"""Persistent watchlist. Only symbols already in this list may be opened."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from app.binance_client import PUBLIC_FAPI, PUBLIC_TIMEOUT, _parse_filters
from app.engine import SymbolFilters

log = logging.getLogger("open-controller")

DEFAULT_SYMBOLS = ["BTCUSDT"]
MAX_WATCHLIST = 50

# (ok, reason_zh, symbol_entry_or_none)
ValidateResult = tuple[bool, str, Optional[dict[str, Any]]]


def normalize_symbol(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper()


def is_symbol_code(symbol: str) -> bool:
    """Allow underscore so delivery codes can be classified and rejected clearly."""
    return bool(symbol) and all(ch.isalnum() or ch in {"_", "-"} for ch in symbol)


def is_usdtm_perpetual(entry: dict[str, Any]) -> bool:
    if not entry:
        return False
    contract = str(entry.get("contractType") or "").upper()
    quote = str(entry.get("quoteAsset") or "").upper()
    status = str(entry.get("status") or "").upper()
    return contract == "PERPETUAL" and quote == "USDT" and status == "TRADING"


def classify_symbol_entry(symbol: str, info: dict[str, Any]) -> ValidateResult:
    target = normalize_symbol(symbol)
    if not is_symbol_code(target):
        return False, "交易对代码无效", None
    symbols = info.get("symbols") or []
    entry = next((s for s in symbols if str(s.get("symbol") or "").upper() == target), None)
    if entry is None:
        return False, "未找到该交易对，请确认是币安 U 本位永续合约代码", None
    contract = str(entry.get("contractType") or "").upper()
    quote = str(entry.get("quoteAsset") or "").upper()
    status = str(entry.get("status") or "").upper()
    if contract != "PERPETUAL" or quote != "USDT":
        return False, "该交易对不是 U 本位永续合约，无法加入自选", entry
    if status and status != "TRADING":
        return False, "该交易对当前不可交易，无法加入自选", entry
    return True, "ok", entry


def fetch_exchange_info(symbol: str, *, http_get: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """Fetch public USD-M exchangeInfo for one symbol. Raises on network failure."""
    import httpx

    url = f"{PUBLIC_FAPI}/fapi/v1/exchangeInfo"
    if http_get is not None:
        return http_get(url, symbol)
    with httpx.Client(timeout=PUBLIC_TIMEOUT) as client:
        resp = client.get(url, params={"symbol": normalize_symbol(symbol)})
        resp.raise_for_status()
        return resp.json()


def validate_usdtm_perp(
    symbol: str,
    *,
    info: Optional[dict[str, Any]] = None,
    http_get: Optional[Callable[..., Any]] = None,
) -> ValidateResult:
    target = normalize_symbol(symbol)
    if not is_symbol_code(target):
        return False, "交易对代码无效", None
    try:
        payload = info if info is not None else fetch_exchange_info(target, http_get=http_get)
    except Exception:
        log.warning("校验自选交易对时无法读取 exchangeInfo: %s", target)
        return False, "无法验证该交易对（网络或交易所接口不可用），未加入自选", None
    return classify_symbol_entry(target, payload)


def filters_from_entry(entry: dict[str, Any]) -> Optional[SymbolFilters]:
    symbol = str(entry.get("symbol") or "")
    return _parse_filters({"symbols": [entry]}, symbol)


class WatchlistStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[str]:
        if not self.path.exists():
            return list(DEFAULT_SYMBOLS)
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            log.warning("读取自选文件失败，将使用默认自选")
            return list(DEFAULT_SYMBOLS)
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("symbols") or []
        else:
            items = []
        seen: set[str] = set()
        out: list[str] = []
        for item in items[:MAX_WATCHLIST]:
            sym = normalize_symbol(str(item))
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out

    def contains(self, symbol: str) -> bool:
        return normalize_symbol(symbol) in set(self.load())

    def _save(self, symbols: list[str]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"symbols": symbols}, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def ensure_file(self) -> list[str]:
        """Materialize the default file if it does not exist yet."""
        symbols = self.load()
        if not self.path.exists():
            self._save(symbols)
        return symbols

    def add(self, symbol: str) -> list[str]:
        sym = normalize_symbol(symbol)
        symbols = self.load()
        if sym and sym not in symbols:
            symbols.append(sym)
            self._save(symbols)
        elif not self.path.exists():
            self._save(symbols)
        return symbols

    def remove(self, symbol: str) -> list[str]:
        sym = normalize_symbol(symbol)
        symbols = [s for s in self.load() if s != sym]
        self._save(symbols)
        return symbols
