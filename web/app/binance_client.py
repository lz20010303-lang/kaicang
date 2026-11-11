"""Binance USD-M futures wrapper: mock (default), testnet, and live.

Trade endpoints are only used when the app explicitly requests an open.
Keys are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.engine import LEVERAGE, MARGIN_MODE, SymbolFilters, _decimal_str

log = logging.getLogger("open-controller")

PUBLIC_FAPI = "https://fapi.binance.com"
TESTNET_FAPI = "https://testnet.binancefuture.com"
LIVE_FAPI = "https://fapi.binance.com"

FALLBACK_MARK = Decimal("100000")
DEFAULT_FILTERS = SymbolFilters(
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("100"),
)

MAX_CLOSED_HISTORY = 200
MAX_ORDER_HISTORY = 600
MAX_OPEN_MOCK_POSITIONS = 20

# Public mark / exchangeInfo fetches are not trades.
PUBLIC_TIMEOUT = 5.0
SIGNED_TIMEOUT = 10.0


class ClientError(Exception):
    def __init__(
        self, message: str, *, code: str = "CLIENT_ERROR", http_status: Optional[int] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class BinanceClient:
    def get_mark_price(self, symbol: str) -> Decimal:
        raise NotImplementedError

    def get_balance(self) -> tuple[Decimal, str]:
        """Return (balance, field_name_for_display)."""
        raise NotImplementedError

    def get_filters(self, symbol: str) -> SymbolFilters:
        raise NotImplementedError

    def ensure_account_settings(self, symbol: str) -> None:
        """Set 20x and CROSSED. No-op in mock."""

    def market_open(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        raise NotImplementedError

    def place_stop(
        self,
        symbol: str,
        position_side: str,
        stop: Decimal,
        quantity: Decimal,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def flatten(self, symbol: str, position_side: str, quantity: Decimal) -> dict[str, Any]:
        raise NotImplementedError

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[list[Any]]:
        """Official USD-M klines (public). Includes the forming candle if Binance returns it."""
        raise NotImplementedError


_public_mark_cache: dict[str, tuple[float, Decimal]] = {}
_PUBLIC_MARK_TTL = 1.5


def _fetch_public_mark(symbol: str) -> Optional[Decimal]:
    cached = _public_mark_cache.get(symbol)
    if cached is not None and time.time() - cached[0] < _PUBLIC_MARK_TTL:
        return cached[1]
    mark = _fetch_public_mark_uncached(symbol)
    if mark is not None:
        _public_mark_cache[symbol] = (time.time(), mark)
    return mark


def _fetch_public_mark_uncached(symbol: str) -> Optional[Decimal]:
    url = f"{PUBLIC_FAPI}/fapi/v1/premiumIndex"
    try:
        with httpx.Client(timeout=PUBLIC_TIMEOUT) as client:
            resp = client.get(url, params={"symbol": symbol})
            resp.raise_for_status()
            data = resp.json()
            mark = Decimal(str(data["markPrice"]))
            if mark > 0 and mark.is_finite():
                return mark
    except Exception:
        log.warning("获取公开标记价格失败，将使用回退价格")
    return None


def _parse_filters(info: dict[str, Any], symbol: str) -> Optional[SymbolFilters]:
    target = symbol.upper()
    symbols = info.get("symbols") or []
    entry = next((s for s in symbols if s.get("symbol") == target), None)
    if not entry:
        return None
    tick = step = min_qty = min_notional = None
    market_step = market_min = None
    for flt in entry.get("filters") or []:
        kind = flt.get("filterType")
        if kind == "PRICE_FILTER":
            tick = flt.get("tickSize")
        elif kind == "LOT_SIZE":
            step = flt.get("stepSize")
            min_qty = flt.get("minQty")
        elif kind == "MARKET_LOT_SIZE":
            market_step = flt.get("stepSize")
            market_min = flt.get("minQty")
        elif kind in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = flt.get("notional") or flt.get("minNotional")
    if market_step:
        try:
            if step is None or Decimal(str(market_step)) < Decimal(str(step)):
                # Use the stricter (smaller) step only if it divides, else keep LOT_SIZE.
                if step is None:
                    step = market_step
        except Exception:
            pass
    if market_min:
        try:
            if min_qty is None or Decimal(str(market_min)) > Decimal(str(min_qty)):
                min_qty = market_min
        except Exception:
            pass
    return SymbolFilters.from_raw(tick, step, min_qty, min_notional)


def _fetch_public_filters(symbol: str) -> Optional[SymbolFilters]:
    url = f"{PUBLIC_FAPI}/fapi/v1/exchangeInfo"
    try:
        with httpx.Client(timeout=PUBLIC_TIMEOUT) as client:
            resp = client.get(url, params={"symbol": symbol})
            resp.raise_for_status()
            return _parse_filters(resp.json(), symbol)
    except Exception:
        log.warning("获取公开合约过滤器失败，将使用回退过滤器")
    return None


# Brief in-process cache for official USD-M klines. Indicators are computed locally.
_kline_cache: dict[tuple[str, str, int], tuple[float, list[list[Any]]]] = {}
_DEFAULT_KLINE_TTL = 15.0


def fetch_public_klines(
    symbol: str,
    interval: str,
    limit: int = 200,
    *,
    ttl: float = _DEFAULT_KLINE_TTL,
    use_cache: bool = True,
) -> list[list[Any]]:
    """GET https://fapi.binance.com/fapi/v1/klines — official public USD-M klines."""
    key = (symbol.upper(), interval, int(limit))
    now = time.time()
    if use_cache:
        cached = _kline_cache.get(key)
        if cached is not None:
            fetched_at, rows = cached
            if (now - fetched_at) < ttl:
                return rows
    url = f"{PUBLIC_FAPI}/fapi/v1/klines"
    try:
        with httpx.Client(timeout=PUBLIC_TIMEOUT) as client:
            resp = client.get(
                url,
                params={"symbol": symbol.upper(), "interval": interval, "limit": int(limit)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise ClientError(f"获取 K 线失败：{exc}", code="KLINES") from exc
    if not isinstance(data, list) or not data:
        raise ClientError("K 线数据为空或无效", code="KLINES")
    if use_cache:
        _kline_cache[key] = (now, data)
    return data


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------


class MockClient(BinanceClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ledger_path: Path = Path(settings.ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._filters_by_symbol: dict[str, SymbolFilters] = {}
        self._mark_override: dict[str, Decimal] = {}
        self._last_marks: dict[str, Decimal] = {}
        self._mark_fetched_at: dict[str, float] = {}
        self._fallback_mark = FALLBACK_MARK
        self._mark_ttl_seconds = 2.0
        self._kline_override: dict[tuple[str, str], list[list[Any]]] = {}

    def _load(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {
                "wallet_balance": str(Decimal(str(self.settings.mock_balance))),
                "positions": [],
                "orders": [],
            }
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        # Bound ledger size: keep every live position, trim closed history.
        positions = data.get("positions") or []
        live = [p for p in positions if p.get("status") in {"open", "unprotected"}]
        closed = [p for p in positions if p.get("status") not in {"open", "unprotected"}]
        if len(closed) > MAX_CLOSED_HISTORY:
            data["positions"] = closed[-MAX_CLOSED_HISTORY:] + live
        orders = data.get("orders") or []
        if len(orders) > MAX_ORDER_HISTORY:
            data["orders"] = orders[-MAX_ORDER_HISTORY:]
        tmp = self.ledger_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.ledger_path)

    def set_filters(self, symbol: str, filters: SymbolFilters) -> None:
        self._filters_by_symbol[symbol.upper()] = filters

    def set_mark(self, symbol: str, mark: Decimal) -> None:
        key = symbol.upper()
        self._mark_override[key] = mark
        self._last_marks[key] = mark

    def set_klines(self, symbol: str, interval: str, rows: list[list[Any]]) -> None:
        self._kline_override[(symbol.upper(), interval)] = rows

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[list[Any]]:
        key = (symbol.upper(), interval)
        if key in self._kline_override:
            rows = self._kline_override[key]
            return rows[-int(limit):] if limit else rows
        ttl = float(getattr(self.settings, "kline_cache_ttl", _DEFAULT_KLINE_TTL) or _DEFAULT_KLINE_TTL)
        want = int(getattr(self.settings, "kline_limit", limit) or limit)
        return fetch_public_klines(symbol, interval, max(want, int(limit)), ttl=ttl)

    def get_filters(self, symbol: str) -> SymbolFilters:
        key = symbol.upper()
        if key not in self._filters_by_symbol:
            self._filters_by_symbol[key] = _fetch_public_filters(key) or DEFAULT_FILTERS
        return self._filters_by_symbol[key]

    def get_mark_price(self, symbol: str) -> Decimal:
        key = symbol.upper()
        if key in self._mark_override:
            return self._mark_override[key]
        now = time.time()
        cached = self._last_marks.get(key)
        fetched_at = self._mark_fetched_at.get(key, 0.0)
        if cached is not None and (now - fetched_at) < self._mark_ttl_seconds:
            return cached
        mark = _fetch_public_mark(key)
        if mark is not None:
            self._last_marks[key] = mark
            self._mark_fetched_at[key] = now
            return mark
        if cached is not None:
            return cached
        return self._fallback_mark

    def _lookup_mark(
        self, symbol: str, marks: Optional[dict[str, Decimal]] = None
    ) -> Decimal:
        key = symbol.upper()
        if marks:
            for name, value in marks.items():
                if name.upper() == key:
                    return value
        return self.get_mark_price(key)

    def _equity(
        self, data: dict[str, Any], marks: Optional[dict[str, Decimal]] = None
    ) -> Decimal:
        wallet = Decimal(str(data.get("wallet_balance", "0")))
        upnl = Decimal("0")
        for pos in data.get("positions") or []:
            if pos.get("status") not in {"open", "unprotected"}:
                continue
            qty = Decimal(str(pos["quantity"]))
            entry = Decimal(str(pos["entry"]))
            mark = self._lookup_mark(str(pos["symbol"]), marks)
            if pos["side"] == "LONG":
                upnl += (mark - entry) * qty
            else:
                upnl += (entry - mark) * qty
        return wallet + upnl

    def get_balance(self) -> tuple[Decimal, str]:
        data = self._load()
        # Each open mock position is marked to its own symbol's price.
        equity = self._equity(data)
        return equity, "模拟权益（wallet + 未实现盈亏）"

    def ensure_account_settings(self, symbol: str) -> None:
        return

    def _used_margin(self, data: dict[str, Any]) -> Decimal:
        used = Decimal("0")
        for pos in data.get("positions") or []:
            if pos.get("status") not in {"open", "unprotected"}:
                continue
            used += Decimal(str(pos["notional"])) / Decimal(LEVERAGE)
        return used

    def market_open(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        data = self._load()
        mark = self.get_mark_price(symbol)
        notional = quantity * mark
        equity = self._equity(data, {symbol: mark})
        available = equity - self._used_margin(data)
        margin = notional / Decimal(LEVERAGE)
        if margin > available:
            raise ClientError(
                f"模拟交易所拒绝：保证金不足（需要 {_decimal_str(margin)} USDT，"
                f"可用 {_decimal_str(available)} USDT）",
                code="INSUFFICIENT_MARGIN",
            )
        live_count = sum(
            1 for p in data.get("positions") or [] if p.get("status") in {"open", "unprotected"}
        )
        if live_count >= MAX_OPEN_MOCK_POSITIONS:
            raise ClientError(
                f"模拟账户最多同时持有 {MAX_OPEN_MOCK_POSITIONS} 个仓位",
                code="MOCK_POSITION_LIMIT",
            )
        order_id = f"mock-entry-{uuid.uuid4().hex[:12]}"
        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": "BUY" if side == "LONG" else "SELL",
            "type": "MARKET",
            "status": "FILLED",
            "avgPrice": _decimal_str(mark),
            "executedQty": _decimal_str(quantity),
            "cumQuote": _decimal_str(notional),
        }
        data.setdefault("orders", []).append(order)
        self._save(data)
        return order

    def place_stop(
        self,
        symbol: str,
        position_side: str,
        stop: Decimal,
        quantity: Decimal,
    ) -> dict[str, Any]:
        data = self._load()
        order_id = f"mock-stop-{uuid.uuid4().hex[:12]}"
        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": "SELL" if position_side == "LONG" else "BUY",
            "type": "STOP_MARKET",
            "status": "NEW",
            "stopPrice": _decimal_str(stop),
            "origQty": _decimal_str(quantity),
            "reduceOnly": True,
        }
        data.setdefault("orders", []).append(order)
        self._save(data)
        return order

    def record_position(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        entry: Decimal,
        stop: Decimal,
        notional: Decimal,
        risk_usd: Decimal,
        entry_order_id: str,
        stop_order_id: Optional[str],
        status: str,
    ) -> dict[str, Any]:
        data = self._load()
        position = {
            "id": f"pos-{uuid.uuid4().hex[:12]}",
            "symbol": symbol,
            "side": side,
            "quantity": _decimal_str(quantity),
            "entry": _decimal_str(entry),
            "stop": _decimal_str(stop),
            "notional": _decimal_str(notional),
            "risk_usd": _decimal_str(risk_usd),
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "status": status,
            "time": int(time.time() * 1000),
        }
        data.setdefault("positions", []).append(position)
        self._save(data)
        return position

    def flatten(self, symbol: str, position_side: str, quantity: Decimal) -> dict[str, Any]:
        data = self._load()
        mark = self.get_mark_price(symbol)
        remaining = quantity
        for pos in reversed(data.get("positions") or []):
            if remaining <= 0:
                break
            if pos.get("symbol") != symbol:
                continue
            if pos.get("side") != position_side:
                continue
            if pos.get("status") not in {"open", "unprotected"}:
                continue
            pos_qty = Decimal(str(pos["quantity"]))
            entry = Decimal(str(pos["entry"]))
            close_qty = min(pos_qty, remaining)
            if position_side == "LONG":
                pnl = (mark - entry) * close_qty
            else:
                pnl = (entry - mark) * close_qty
            wallet = Decimal(str(data["wallet_balance"]))
            data["wallet_balance"] = _decimal_str(wallet + pnl)
            leftover = pos_qty - close_qty
            if leftover <= 0:
                pos["status"] = "flattened"
            else:
                pos["quantity"] = _decimal_str(leftover)
                pos["notional"] = _decimal_str(leftover * entry)
            remaining -= close_qty
        if remaining > 0:
            raise ClientError("模拟平仓失败：找不到对应仓位", code="FLATTEN_FAILED")
        order_id = f"mock-flat-{uuid.uuid4().hex[:12]}"
        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": "SELL" if position_side == "LONG" else "BUY",
            "type": "MARKET",
            "status": "FILLED",
            "reduceOnly": True,
            "executedQty": _decimal_str(quantity),
            "avgPrice": _decimal_str(mark),
        }
        data.setdefault("orders", []).append(order)
        self._save(data)
        return order


    def get_positions(self, marks: Optional[dict[str, Decimal]] = None) -> list[dict[str, Any]]:
        data = self._load()
        out: list[dict[str, Any]] = []
        for pos in data.get("positions") or []:
            status = pos.get("status")
            if status not in {"open", "unprotected"}:
                continue
            symbol = str(pos["symbol"])
            mark = self._lookup_mark(symbol, marks)
            qty = Decimal(str(pos["quantity"]))
            entry = Decimal(str(pos["entry"]))
            side = pos["side"]
            if side == "LONG":
                upnl = (mark - entry) * qty
            else:
                upnl = (entry - mark) * qty
            stop_order_id = pos.get("stop_order_id")
            protected = status == "open" and bool(stop_order_id)
            out.append(
                {
                    "id": pos.get("id"),
                    "symbol": symbol,
                    "side": side,
                    "quantity": pos["quantity"],
                    "entry": pos["entry"],
                    "mark": _decimal_str(mark),
                    "unrealized_pnl": _decimal_str(upnl),
                    "stop": pos.get("stop"),
                    "stop_order_id": stop_order_id,
                    "stop_resting": protected,
                    "protected": protected,
                    "status": "open" if protected else "unprotected",
                }
            )
        return out

    def apply_stop_triggers(
        self, marks: Optional[dict[str, Decimal]] = None
    ) -> list[dict[str, Any]]:
        """Close protected mock positions whose mark has crossed the stop.

        Booking assumption (mock only): trigger when mark crosses the stop
        (long: mark <= stop, short: mark >= stop) and fill at that same
        mark. This is a ledger booking rule, not a guaranteed live fill.
        Unprotected positions are not auto-closed here; the open-time
        flatten / failure path still applies.
        """
        data = self._load()
        closed: list[dict[str, Any]] = []
        for pos in data.get("positions") or []:
            if pos.get("status") != "open":
                continue
            if not pos.get("stop_order_id") or pos.get("stop") in (None, ""):
                continue
            symbol = str(pos["symbol"])
            mark = self._lookup_mark(symbol, marks)
            stop = Decimal(str(pos["stop"]))
            side = pos["side"]
            hit = (side == "LONG" and mark <= stop) or (side == "SHORT" and mark >= stop)
            if not hit:
                continue
            qty = Decimal(str(pos["quantity"]))
            entry = Decimal(str(pos["entry"]))
            pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
            wallet = Decimal(str(data["wallet_balance"]))
            data["wallet_balance"] = _decimal_str(wallet + pnl)
            pos["status"] = "stopped"
            pos["close_price"] = _decimal_str(mark)
            pos["realized_pnl"] = _decimal_str(pnl)
            pos["close_time"] = int(time.time() * 1000)
            pos["close_reason"] = "stop_triggered"
            stop_id = str(pos.get("stop_order_id") or "")
            for order in data.get("orders") or []:
                if str(order.get("orderId")) == stop_id:
                    order["status"] = "FILLED"
                    order["avgPrice"] = _decimal_str(mark)
                    order["executedQty"] = _decimal_str(qty)
                    break
            closed.append(
                {
                    "id": pos.get("id"),
                    "symbol": symbol,
                    "side": side,
                    "quantity": _decimal_str(qty),
                    "entry": _decimal_str(entry),
                    "stop": _decimal_str(stop),
                    "fill_price": _decimal_str(mark),
                    "realized_pnl": _decimal_str(pnl),
                    "wallet_balance": data["wallet_balance"],
                }
            )
        if closed:
            self._save(data)
        return closed


# ---------------------------------------------------------------------------
# Signed live / testnet
# ---------------------------------------------------------------------------


_SHARED_SIGNED_FILTERS: dict[str, dict[str, SymbolFilters]] = {}


class SignedClient(BinanceClient):
    """Built per request from the caller's own key/secret; the server keeps neither."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = TESTNET_FAPI if settings.mode == "testnet" else LIVE_FAPI
        # Filters are public; share them across per-request clients.
        self._filters_by_symbol = _SHARED_SIGNED_FILTERS.setdefault(self.base, {})
        self._key = settings.binance_api_key or ""
        self._secret = settings.binance_api_secret or ""

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._key}

    def _signed_params(self, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = 5000
        query = urlencode(payload, doseq=True)
        signature = hmac.new(
            self._secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["signature"] = signature
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        url = f"{self.base}{path}"
        send = self._signed_params(params) if signed else (params or {})
        try:
            with httpx.Client(timeout=SIGNED_TIMEOUT) as client:
                resp = client.request(method, url, params=send, headers=self._headers())
        except httpx.HTTPError as exc:
            # Do not echo the URL (it carries the signed query string).
            raise ClientError(f"连接币安失败：{type(exc).__name__}", code="NETWORK") from None
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("msg") or str(body)
                code = body.get("code")
            except Exception:
                msg = resp.text
                code = resp.status_code
            if code in (None, 0, "0"):
                code = resp.status_code
            raise ClientError(
                f"币安接口错误 ({code}): {msg}", code=str(code), http_status=resp.status_code
            )
        return resp.json()

    def verify(self) -> dict[str, Any]:
        """Signed read-only check (GET /fapi/v2/balance). Never places orders."""
        rows = self._request("GET", "/fapi/v2/balance", signed=True)
        usdt = None
        for row in rows or []:
            if str(row.get("asset") or "") == "USDT":
                usdt = row.get("balance")
                break
        return {"usdt_balance": None if usdt is None else str(usdt)}

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[list[Any]]:
        # Official USD-M market data, not the testnet kline book.
        ttl = float(getattr(self.settings, "kline_cache_ttl", _DEFAULT_KLINE_TTL) or _DEFAULT_KLINE_TTL)
        want = int(getattr(self.settings, "kline_limit", limit) or limit)
        return fetch_public_klines(symbol, interval, max(want, int(limit)), ttl=ttl)

    def get_mark_price(self, symbol: str) -> Decimal:
        data = self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol},
        )
        mark = Decimal(str(data["markPrice"]))
        if mark <= 0 or not mark.is_finite():
            raise ClientError("标记价格无效", code="INVALID_MARK")
        return mark

    def get_filters(self, symbol: str) -> SymbolFilters:
        key = symbol.upper()
        if key not in self._filters_by_symbol:
            info = self._request("GET", "/fapi/v1/exchangeInfo", params={"symbol": key})
            parsed = _parse_filters(info, key)
            if parsed is None:
                raise ClientError("无法读取合约过滤器", code="FILTERS")
            self._filters_by_symbol[key] = parsed
        return self._filters_by_symbol[key]

    def get_balance(self) -> tuple[Decimal, str]:
        data = self._request("GET", "/fapi/v2/account", signed=True)
        raw = data.get("totalMarginBalance")
        if raw is None:
            raw = data.get("totalWalletBalance")
            if raw is None:
                raise ClientError("账户信息中没有保证金余额", code="NO_BALANCE")
            return Decimal(str(raw)), "totalWalletBalance（钱包余额）"
        return Decimal(str(raw)), "totalMarginBalance（保证金余额/权益）"

    def ensure_account_settings(self, symbol: str) -> None:
        try:
            self._request(
                "POST",
                "/fapi/v1/marginType",
                params={"symbol": symbol, "marginType": "CROSSED"},
                signed=True,
            )
        except ClientError as exc:
            # -4046: already CROSSED
            if "-4046" not in str(exc.code) and "4046" not in str(exc.code):
                text = (exc.message or "").lower()
                if "no need to change" not in text and "already" not in text:
                    raise
        self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": LEVERAGE},
            signed=True,
        )

    def market_open(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": "BUY" if side == "LONG" else "SELL",
                "type": "MARKET",
                "quantity": _decimal_str(quantity),
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    def place_stop(
        self,
        symbol: str,
        position_side: str,
        stop: Decimal,
        quantity: Decimal,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": "SELL" if position_side == "LONG" else "BUY",
                "type": "STOP_MARKET",
                "stopPrice": _decimal_str(stop),
                "quantity": _decimal_str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    def flatten(self, symbol: str, position_side: str, quantity: Decimal) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": "SELL" if position_side == "LONG" else "BUY",
                "type": "MARKET",
                "quantity": _decimal_str(quantity),
                "reduceOnly": "true",
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    def get_positions(self) -> list[dict[str, Any]]:
        raw_positions = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        try:
            open_orders = self._request("GET", "/fapi/v1/openOrders", signed=True)
        except ClientError:
            open_orders = []
        if not isinstance(open_orders, list):
            open_orders = []
        out: list[dict[str, Any]] = []
        for item in raw_positions or []:
            try:
                amt = Decimal(str(item.get("positionAmt") or "0"))
            except Exception:
                continue
            if amt == 0:
                continue
            side = "LONG" if amt > 0 else "SHORT"
            qty = abs(amt)
            symbol = str(item.get("symbol") or "")
            entry = Decimal(str(item.get("entryPrice") or "0"))
            mark = Decimal(str(item.get("markPrice") or "0"))
            try:
                upnl = Decimal(str(item.get("unRealizedProfit") or "0"))
            except Exception:
                if side == "LONG":
                    upnl = (mark - entry) * qty
                else:
                    upnl = (entry - mark) * qty
            want_side = "SELL" if side == "LONG" else "BUY"
            stop_price = None
            stop_order_id = None
            stop_resting = False
            for order in open_orders:
                if str(order.get("symbol") or "") != symbol:
                    continue
                reduce_only = order.get("reduceOnly")
                if reduce_only is not True and str(reduce_only).lower() != "true":
                    continue
                typ = str(order.get("type") or "").upper()
                if typ not in {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"}:
                    continue
                if str(order.get("side") or "").upper() != want_side:
                    continue
                stop_price = order.get("stopPrice")
                stop_order_id = order.get("orderId")
                stop_resting = True
                break
            protected = stop_resting
            out.append(
                {
                    "id": None,
                    "symbol": symbol,
                    "side": side,
                    "quantity": _decimal_str(qty),
                    "entry": _decimal_str(entry),
                    "mark": _decimal_str(mark),
                    "unrealized_pnl": _decimal_str(upnl),
                    "stop": None if stop_price is None else str(stop_price),
                    "stop_order_id": None if stop_order_id is None else str(stop_order_id),
                    "stop_resting": stop_resting,
                    "protected": protected,
                    "status": "open" if protected else "unprotected",
                }
            )
        return out


def make_client(settings: Settings) -> BinanceClient:
    if settings.mode == "mock":
        return MockClient(settings)
    return SignedClient(settings)


def fill_price(order: dict[str, Any], fallback: Decimal) -> Decimal:
    for key in ("avgPrice", "price"):
        raw = order.get(key)
        if raw not in (None, "", "0"):
            try:
                value = Decimal(str(raw))
                if value > 0:
                    return value
            except Exception:
                continue
    return fallback
