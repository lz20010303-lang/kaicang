"""FastAPI entry: local Binance USD-M opening controller."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.binance_client import (
    BinanceClient,
    ClientError,
    MockClient,
    SignedClient,
    TESTNET_FAPI,
    LIVE_FAPI,
    fill_price,
    make_client,
)
from app.config import Settings, load_settings
from app.engine import LEVERAGE, MARGIN_MODE, _normalize_side, evaluate, require_in_watchlist
from app.indicators import (
    INTERVALS,
    SOURCE_MA,
    SOURCE_SUPERTREND,
    closed_candles,
    compute_indicator,
    ma_params_label,
    normalize_interval,
    normalize_source,
    suggest_stop,
    supertrend_params_label,
)
from app.macd import (
    DIVERGENCE_METRIC,
    LOWER_INTERVAL,
    analyze_macd,
    empty_failure,
    lower_interval,
    macd_interval_label,
    macd_params_label,
)
from app.models import OpenRequest, WatchlistAddRequest
from app.oplog import OpLog
from app import region_probe
from app.security import (
    HEADER_CLIENT_ID,
    HEADER_KEY,
    HEADER_MODE,
    HEADER_SECRET,
    WRITE_PATHS,
    RateLimiter,
    client_ip,
    egress_ip,
    explain_binance_error,
    mask_key,
    valid_api_credential,
    valid_client_id,
)
from app.watchlist import (
    MAX_WATCHLIST,
    WatchlistStore,
    filters_from_entry,
    normalize_symbol,
    validate_usdtm_perp,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # signed URLs carry signature/timestamp
log = logging.getLogger("open-controller")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_settings: Optional[Settings] = None
_client: Optional[BinanceClient] = None
_watchlist: Optional[WatchlistStore] = None
_oplog: Optional[OpLog] = None
_watcher_task: Optional[asyncio.Task] = None
_probe_task: Optional[asyncio.Task] = None
_mock_clients: dict[str, MockClient] = {}
_limiter = RateLimiter()
MAX_CACHED_MOCK_CLIENTS = 5000


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def get_client() -> BinanceClient:
    global _client
    if _client is None:
        _client = make_client(get_settings())
    return _client


def get_watchlist() -> WatchlistStore:
    global _watchlist
    if _watchlist is None:
        store = WatchlistStore(get_settings().watchlist_path)
        store.ensure_file()
        _watchlist = store
    return _watchlist


def get_oplog() -> OpLog:
    global _oplog
    if _oplog is None:
        _oplog = OpLog(get_settings().oplog_path)
    return _oplog


def _indicator_meta(settings: Settings) -> dict:
    return {
        "intervals": [{"id": item[0], "label": item[1]} for item in INTERVALS],
        "indicators": {
            "ma": {
                "id": SOURCE_MA,
                "label": "均线",
                "params_label": ma_params_label(settings.ma_period),
                "period": settings.ma_period,
                "type": "SMA",
            },
            "supertrend": {
                "id": SOURCE_SUPERTREND,
                "label": "Supertrend",
                "params_label": supertrend_params_label(
                    settings.supertrend_atr_period, settings.supertrend_multiplier
                ),
                "atr_period": settings.supertrend_atr_period,
                "multiplier": str(settings.supertrend_multiplier),
            },
        },
        "default_interval": "15m",
        "macd": {
            "params_label": macd_params_label(
                settings.macd_fast, settings.macd_slow, settings.macd_signal
            ),
            "fast": settings.macd_fast,
            "slow": settings.macd_slow,
            "signal": settings.macd_signal,
            "lookback": settings.macd_divergence_lookback,
            "metric": DIVERGENCE_METRIC,
            "lower_intervals": dict(LOWER_INTERVAL),
            "reference_only": True,
            "note_zh": "仅供参考，不影响开单",
        },
    }


def _evaluate_indicator_fill(
    *,
    settings: Settings,
    client: BinanceClient,
    symbol: str,
    interval: str,
    source: str,
    side: Optional[str],
) -> dict:
    params_label = (
        ma_params_label(settings.ma_period)
        if source == SOURCE_MA
        else supertrend_params_label(
            settings.supertrend_atr_period, settings.supertrend_multiplier
        )
    )
    mark = None
    tick = None
    try:
        mark = client.get_mark_price(symbol)
        filters = client.get_filters(symbol)
        tick = filters.tick_size
    except ClientError as exc:
        fill = suggest_stop(
            interval=interval,
            source=source,
            value=None,
            mark=None,
            side=side,
            tick_size=None,
            params_label=params_label,
            error=f"读取标记价格或过滤器失败：{exc.message}",
        )
        return fill.to_dict()
    except Exception as exc:
        fill = suggest_stop(
            interval=interval,
            source=source,
            value=None,
            mark=None,
            side=side,
            tick_size=None,
            params_label=params_label,
            error=f"读取标记价格或过滤器失败：{exc}",
        )
        return fill.to_dict()

    try:
        rows = client.get_klines(symbol, interval, limit=int(settings.kline_limit))
    except ClientError as exc:
        return suggest_stop(
            interval=interval,
            source=source,
            value=None,
            mark=mark,
            side=side,
            tick_size=tick,
            params_label=params_label,
            error=exc.message,
        ).to_dict()
    except Exception as exc:
        return suggest_stop(
            interval=interval,
            source=source,
            value=None,
            mark=mark,
            side=side,
            tick_size=tick,
            params_label=params_label,
            error=f"获取 K 线失败：{exc}",
        ).to_dict()

    candles = closed_candles(rows)
    if not candles:
        return suggest_stop(
            interval=interval,
            source=source,
            value=None,
            mark=mark,
            side=side,
            tick_size=tick,
            params_label=params_label,
            error="没有已收盘的 K 线，无法计算指标（不会使用未收盘的当前 K 线）",
        ).to_dict()

    computed = compute_indicator(
        candles,
        source,
        ma_period=settings.ma_period,
        supertrend_atr_period=settings.supertrend_atr_period,
        supertrend_multiplier=settings.supertrend_multiplier,
    )
    return suggest_stop(
        interval=interval,
        source=source,
        value=computed.value,
        mark=mark,
        side=side,
        tick_size=tick,
        candle=computed.candle,
        params_label=params_label,
        error=None if computed.ok else computed.reason_zh,
    ).to_dict()


def _macd_kline_limit(settings: Settings) -> int:
    need = (
        int(settings.macd_divergence_lookback)
        + int(settings.macd_slow)
        + int(settings.macd_signal)
        + 20
    )
    return max(int(settings.kline_limit), need, 200)


def _fetch_closed_klines(client: BinanceClient, symbol: str, interval: str, limit: int):
    rows = client.get_klines(symbol, interval, limit=limit)
    return closed_candles(rows)


def _evaluate_macd(
    *,
    settings: Settings,
    client: BinanceClient,
    symbol: str,
    interval: str,
) -> dict:
    lower = lower_interval(interval)
    params_label = macd_params_label(
        settings.macd_fast, settings.macd_slow, settings.macd_signal
    )
    limit = _macd_kline_limit(settings)
    kwargs = dict(
        interval=interval,
        lower=lower,
        reason_zh="",
        params_label=params_label,
        fast=settings.macd_fast,
        slow=settings.macd_slow,
        signal=settings.macd_signal,
        lookback=settings.macd_divergence_lookback,
        symbol=symbol,
    )
    if lower is None:
        return empty_failure(**{**kwargs, "reason_zh": "周期没有对应的小级别"})

    interval_error = None
    lower_error = None
    interval_candles = []
    lower_candles = []
    try:
        interval_candles = _fetch_closed_klines(client, symbol, interval, limit)
    except ClientError as exc:
        interval_error = exc.message
    except Exception as exc:
        interval_error = f"获取 K 线失败：{exc}"
    try:
        lower_candles = _fetch_closed_klines(client, symbol, lower, limit)
    except ClientError as exc:
        lower_error = exc.message
    except Exception as exc:
        lower_error = f"获取 K 线失败：{exc}"

    if interval_error and lower_error:
        return empty_failure(
            **{**kwargs, "reason_zh": f"获取失败：当前周期 {interval_error}；小级别 {lower_error}"}
        )
    if interval_error:
        report = empty_failure(
            **{**kwargs, "reason_zh": f"获取失败：{interval_error}", "cross_failed": True, "div_failed": False}
        )
        # Still try to compute divergence from whatever lower candles we have.
        if not lower_error and lower_candles:
            filled = analyze_macd(
                interval_candles=[],
                lower_candles=lower_candles,
                interval=interval,
                lower=lower,
                fast=settings.macd_fast,
                slow=settings.macd_slow,
                signal=settings.macd_signal,
                lookback=settings.macd_divergence_lookback,
                symbol=symbol,
            )
            report["divergence"] = filled["divergence"]
            report["ok"] = False
        else:
            if lower_error:
                report["divergence"]["reason_zh"] = f"获取失败：{lower_error}"
                report["divergence"]["ok"] = False
                report["divergence"]["label_zh"] = f"{lower} 小级别：获取失败"
        return report
    if lower_error:
        filled = analyze_macd(
            interval_candles=interval_candles,
            lower_candles=[],
            interval=interval,
            lower=lower,
            fast=settings.macd_fast,
            slow=settings.macd_slow,
            signal=settings.macd_signal,
            lookback=settings.macd_divergence_lookback,
            symbol=symbol,
        )
        filled["ok"] = False
        filled["reason_zh"] = f"获取失败：{lower_error}"
        filled["divergence"] = {
            "ok": False,
            "type": None,
            "type_zh": "获取失败",
            "bias": None,
            "interval": lower,
            "interval_label": macd_interval_label(lower),
            "label_zh": f"{lower} 小级别：获取失败",
            "reason_zh": f"获取失败：{lower_error}",
        }
        return filled

    return analyze_macd(
        interval_candles=interval_candles,
        lower_candles=lower_candles,
        interval=interval,
        lower=lower,
        fast=settings.macd_fast,
        slow=settings.macd_slow,
        signal=settings.macd_signal,
        lookback=settings.macd_divergence_lookback,
        symbol=symbol,
    )


def reset_singletons() -> None:
    """Test helper: drop cached settings / client / stores."""
    global _settings, _client, _watchlist, _oplog
    _settings = None
    _client = None
    _watchlist = None
    _oplog = None
    _mock_clients.clear()
    _limiter.reset()


# ---------------------------------------------------------------------------
# Per-browser context. Each browser sends a random X-Client-Id; its watchlist,
# op log and mock ledger live under clients_dir/<client_id>/. For testnet/live
# the browser sends its own key/secret in headers on each request; the server
# only uses them in memory to sign that request and never stores them.
# Requests without X-Client-Id use the single local (legacy) state.
# ---------------------------------------------------------------------------


@dataclass
class RequestContext:
    settings: Settings
    client: BinanceClient
    watchlist: WatchlistStore
    oplog: OpLog
    client_id: Optional[str]
    mode: str
    api_key: Optional[str] = None


def _bad_request(code: str, reason: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason_code": code, "reason_zh": reason})


def client_dir(settings: Settings, client_id: str) -> Path:
    if not valid_client_id(client_id):
        raise _bad_request("INVALID_CLIENT_ID", "客户端标识无效，请清除浏览器数据后刷新")
    base = Path(settings.clients_dir).resolve()
    path = (base / client_id).resolve()
    if path.parent != base:
        raise _bad_request("INVALID_CLIENT_ID", "客户端标识无效")
    return path


def _ensure_capacity(settings: Settings, path: Path) -> None:
    if path.exists():
        return
    base = Path(settings.clients_dir)
    count = sum(1 for p in base.iterdir() if p.is_dir()) if base.exists() else 0
    if count >= int(settings.max_clients):
        raise _bad_request("CLIENTS_FULL", "服务器上的模拟账户数量已达上限，请稍后再试", 503)


def mock_client_for(client_id: str, settings: Optional[Settings] = None) -> MockClient:
    settings = settings or get_settings()
    cached = _mock_clients.get(client_id)
    if cached is not None:
        return cached
    path = client_dir(settings, client_id)
    scoped = settings.model_copy(
        update={
            "mode": "mock",
            "ledger_path": path / "ledger.json",
            "binance_api_key": None,
            "binance_api_secret": None,
        }
    )
    client = MockClient(scoped)
    if len(_mock_clients) >= MAX_CACHED_MOCK_CLIENTS:
        _mock_clients.clear()
    _mock_clients[client_id] = client
    return client


def resolve_context(request: Request) -> RequestContext:
    cached = getattr(request.state, "ctx", None)
    if cached is not None:
        return cached
    settings = get_settings()
    cid = request.headers.get(HEADER_CLIENT_ID)
    if cid is None:
        ctx = RequestContext(
            settings=settings,
            client=get_client(),
            watchlist=get_watchlist(),
            oplog=get_oplog(),
            client_id=None,
            mode=settings.mode,
        )
        request.state.ctx = ctx
        return ctx

    path = client_dir(settings, cid.strip())
    mode = (request.headers.get(HEADER_MODE) or "mock").strip().lower()
    if mode not in {"mock", "testnet", "live"}:
        raise _bad_request("INVALID_MODE", "环境无效，只能是模拟、测试网或实盘")
    if mode == "live" and not settings.live_allowed:
        raise _bad_request(
            "LIVE_DISABLED",
            "服务端未开启实盘（需要运行服务的人设置 ALLOW_LIVE=yes 后重启）。请先用模拟或测试网。",
            403,
        )
    _ensure_capacity(settings, path)
    api_key = None
    if mode == "mock":
        client: BinanceClient = mock_client_for(cid, settings)
    else:
        api_key = (request.headers.get(HEADER_KEY) or "").strip()
        api_secret = (request.headers.get(HEADER_SECRET) or "").strip()
        if not valid_api_credential(api_key) or not valid_api_credential(api_secret):
            raise _bad_request(
                "API_KEY_REQUIRED",
                "测试网/实盘需要先在下方「API 设置」填写你自己的 API Key 和 Secret",
            )
        client = SignedClient(
            settings.model_copy(
                update={"mode": mode, "binance_api_key": api_key, "binance_api_secret": api_secret}
            )
        )
        del api_secret
    path.mkdir(parents=True, exist_ok=True)
    ctx = RequestContext(
        settings=settings.model_copy(update={"mode": mode}),
        client=client,
        watchlist=WatchlistStore(path / "watchlist.json"),
        oplog=OpLog(path / "ops.jsonl"),
        client_id=cid,
        mode=mode,
        api_key=api_key,
    )
    request.state.ctx = ctx
    return ctx


def request_settings(request: Request) -> Settings:
    return resolve_context(request).settings


def request_client(request: Request) -> BinanceClient:
    return resolve_context(request).client


def request_watchlist(request: Request) -> WatchlistStore:
    return resolve_context(request).watchlist


def request_oplog(request: Request) -> OpLog:
    return resolve_context(request).oplog



def process_mock_stops(client: MockClient, oplog: Optional[OpLog] = None) -> list[dict]:
    closed = client.apply_stop_triggers()
    if oplog:
        for item in closed:
            oplog.append(
                "stop_triggered",
                symbol=item.get("symbol"),
                side=item.get("side"),
                quantity=item.get("quantity"),
                stop=item.get("stop"),
                fill_price=item.get("fill_price"),
                realized_pnl=item.get("realized_pnl"),
                reason_zh=(
                    "模拟止损触发：按标记价穿越记账，成交价按当时标记价格记入台账"
                    f"（{_pnl_text(item.get('realized_pnl'))}）"
                ),
            )
    return closed


def _pnl_text(value: object) -> str:
    text = str(value or "0")
    try:
        number = Decimal(text)
        if number > 0:
            return f"盈亏 +{text} USDT"
        return f"盈亏 {text} USDT"
    except Exception:
        return f"盈亏 {text} USDT"


def sweep_mock_stops() -> list[dict]:
    """Apply mock stop triggers for the local ledger and every client ledger."""
    closed_all: list[dict] = []
    settings = get_settings()
    client = get_client()
    if isinstance(client, MockClient):
        closed_all.extend(process_mock_stops(client, get_oplog()))
    base = Path(settings.clients_dir)
    if base.exists():
        for path in base.iterdir():
            if not path.is_dir() or not valid_client_id(path.name):
                continue
            if not (path / "ledger.json").exists():
                continue
            try:
                mock = mock_client_for(path.name, settings)
                items = process_mock_stops(mock, OpLog(path / "ops.jsonl"))
                for item in items:
                    item["client"] = path.name[:6]
                closed_all.extend(items)
            except Exception:
                log.exception("模拟止损扫描出错 client=%s", path.name[:6])
    return closed_all


async def _mock_stop_loop() -> None:
    # Poll frequently enough for a demo, without hammering public APIs.
    interval = 2.0
    while True:
        try:
            await asyncio.sleep(interval)
            closed = await asyncio.to_thread(sweep_mock_stops)
            for item in closed:
                log.info(
                    "模拟止损触发 symbol=%s side=%s fill=%s pnl=%s",
                    item.get("symbol"),
                    item.get("side"),
                    item.get("fill_price"),
                    item.get("realized_pnl"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("模拟止损观察任务出错")


async def _region_probe_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(region_probe.run_probe)
            log.info(
                "币安地区自检 status=%s public=%s signed=%s",
                result.get("status"),
                (result.get("public") or {}).get("http_status"),
                (result.get("signed") or {}).get("http_status"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("币安地区自检出错")
        await asyncio.sleep(region_probe.RECHECK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watcher_task, _probe_task
    settings = get_settings()
    get_client()
    get_watchlist()
    get_oplog()
    # Always run: per-browser mock ledgers exist regardless of the server MODE.
    _watcher_task = asyncio.create_task(_mock_stop_loop())
    if settings.region_probe_enabled:
        _probe_task = asyncio.create_task(_region_probe_loop())
    log.info(
        "开仓控制器启动 mode=%s symbol=%s host=%s port=%s auth_required=%s",
        settings.mode,
        settings.symbol,
        settings.host,
        settings.port,
        settings.auth_required,
    )
    yield
    if _probe_task is not None:
        _probe_task.cancel()
        _probe_task = None
    if _watcher_task is not None:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
        _watcher_task = None


app = FastAPI(title="开仓控制器", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def api_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        settings = get_settings()
        ip = client_ip(request, settings.proxy_trusted)
        if not _limiter.allow("all", ip, int(settings.rate_limit_per_minute)):
            return JSONResponse(
                status_code=429,
                content={"detail": {"reason_code": "RATE_LIMITED", "reason_zh": "请求过于频繁，请稍后再试"}},
            )
        if request.method in {"POST", "DELETE"} and path.startswith(WRITE_PATHS):
            if not _limiter.allow("write", ip, int(settings.rate_limit_write_per_minute)):
                return JSONResponse(
                    status_code=429,
                    content={"detail": {"reason_code": "RATE_LIMITED", "reason_zh": "操作过于频繁，请稍后再试"}},
                )
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def require_token(
    request: Request,
    x_app_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.auth_required:
        return
    expected = settings.app_token or ""
    provided = x_app_token
    if not provided and authorization:
        prefix = "bearer "
        if authorization.lower().startswith(prefix):
            provided = authorization[len(prefix) :].strip()
        else:
            provided = authorization.strip()
    if not provided:
        provided = request.query_params.get("token")
    if provided != expected:
        raise HTTPException(
            status_code=401,
            detail={"reason_code": "UNAUTHORIZED", "reason_zh": "需要有效的访问令牌"},
        )


def _resolve_symbol(requested: Optional[str], settings: Settings) -> str:
    symbol = normalize_symbol(requested)
    return symbol or settings.symbol


def _safe_positions(client: BinanceClient) -> list[dict]:
    try:
        return client.get_positions()
    except ClientError as exc:
        log.warning("读取持仓失败：%s", exc.message)
        return []
    except Exception:
        log.exception("读取持仓失败")
        return []


def _snapshot(
    settings: Settings,
    client: BinanceClient,
    watchlist: WatchlistStore,
    oplog: OpLog,
    *,
    stop=None,
    side=None,
    symbol: Optional[str] = None,
    include_preview: bool = True,
) -> dict:
    current = _resolve_symbol(symbol, settings)
    symbols = watchlist.load()
    if not symbols and not watchlist.path.exists():
        symbols = watchlist.ensure_file()
    mark = client.get_mark_price(current)
    balance, balance_field = client.get_balance()
    filters = client.get_filters(current)
    positions = _safe_positions(client)
    has_unprotected = any(not p.get("protected") for p in positions)
    payload = {
        "mode": settings.mode,
        "symbol": current,
        "in_watchlist": watchlist.contains(current),
        "watchlist": symbols,
        "leverage": LEVERAGE,
        "margin_mode": MARGIN_MODE,
        "margin_mode_zh": "全仓",
        "balance": str(balance),
        "balance_field": balance_field,
        "mark": str(mark),
        "filters": {
            "tick_size": str(filters.tick_size),
            "step_size": str(filters.step_size),
            "min_qty": str(filters.min_qty),
            "min_notional": str(filters.min_notional),
        },
        "risk_fraction": "0.05",
        "auth_required": settings.auth_required,
        "live_allowed": settings.live_allowed,
        "positions": positions,
        "has_unprotected": has_unprotected,
        "logs": oplog.read(limit=50),
        "preview": None,
        "fill_assumption_zh": (
            "模拟模式记账假设：标记价格穿越止损即触发，成交价按当时标记价格记入台账；"
            "这不是实盘成交保证。测试网/实盘不虚构成交，只展示交易所持仓与止损状态。"
        ),
        **_indicator_meta(settings),
    }
    if include_preview and (side is not None or (stop is not None and stop != "")):
        gate = require_in_watchlist(current, symbols)
        if gate is not None:
            payload["preview"] = gate.to_dict()
        else:
            decision = evaluate(
                side=side,
                stop=stop,
                mark=mark,
                balance=balance,
                filters=filters,
            )
            payload["preview"] = decision.to_dict()
    return payload


@app.get("/healthz")
def healthz(settings: Settings = Depends(get_settings)):
    """Liveness for the hosting platform. No network calls, no secrets."""
    return {"ok": True, "mode": settings.mode, "region": region_probe.snapshot().get("status")}


def _region_payload(settings: Settings) -> dict:
    data = region_probe.snapshot()
    if not settings.region_probe_enabled:
        data = {**data, "status": "disabled", "message_zh": "地区自检已关闭（REGION_PROBE=no）"}
    return data


def _storage_payload(settings: Settings) -> dict:
    if settings.is_ephemeral:
        note = (
            "本服务部署在免费云平台，磁盘是临时的：服务重启、重新部署或空闲休眠后，"
            "服务器上的模拟账本、自选列表和操作日志都会清空。API Key 只在你的浏览器里，不受影响。"
        )
    else:
        note = "数据保存在运行服务的电脑的 data/ 目录。"
    return {"ephemeral": settings.is_ephemeral, "storage_note_zh": note}


@app.get("/api/region-check")
def region_check(settings: Settings = Depends(get_settings)):
    return _region_payload(settings)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def meta(
    settings: Settings = Depends(request_settings),
    watchlist: WatchlistStore = Depends(request_watchlist),
):
    return {
        "auth_required": settings.auth_required,
        "symbol": settings.symbol,
        "watchlist": watchlist.load(),
        "mode": settings.mode,
        "leverage": LEVERAGE,
        "margin_mode": MARGIN_MODE,
        **_indicator_meta(settings),
    }


@app.get("/api/state")
def state(
    stop: Optional[str] = Query(default=None),
    side: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(require_token),
    settings: Settings = Depends(request_settings),
    client: BinanceClient = Depends(request_client),
    watchlist: WatchlistStore = Depends(request_watchlist),
    oplog: OpLog = Depends(request_oplog),
):
    try:
        return _snapshot(
            settings,
            client,
            watchlist,
            oplog,
            stop=stop,
            side=side,
            symbol=symbol,
        )
    except ClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"reason_code": exc.code, "reason_zh": exc.message},
        ) from exc


@app.get("/api/positions")
def positions(
    _: None = Depends(require_token),
    client: BinanceClient = Depends(request_client),
):
    try:
        items = client.get_positions()
    except ClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"reason_code": exc.code, "reason_zh": exc.message},
        ) from exc
    return {
        "positions": items,
        "has_unprotected": any(not p.get("protected") for p in items),
    }


@app.get("/api/logs")
def logs(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_token),
    oplog: OpLog = Depends(request_oplog),
):
    return {"logs": oplog.read(limit=limit)}


@app.get("/api/watchlist")
def watchlist_get(
    _: None = Depends(require_token),
    watchlist: WatchlistStore = Depends(request_watchlist),
):
    return {"watchlist": watchlist.load()}


@app.post("/api/watchlist")
def watchlist_add(
    body: WatchlistAddRequest,
    _: None = Depends(require_token),
    client: BinanceClient = Depends(request_client),
    watchlist: WatchlistStore = Depends(request_watchlist),
    oplog: OpLog = Depends(request_oplog),
):
    symbol = normalize_symbol(body.symbol)
    ok, reason, entry = validate_usdtm_perp(symbol)
    if not ok:
        oplog.append(
            "reject",
            event_detail="watchlist_add",
            symbol=symbol or body.symbol,
            reason_code="WATCHLIST_ADD_REJECTED",
            reason_zh=reason,
        )
        raise HTTPException(
            status_code=400,
            detail={"reason_code": "WATCHLIST_ADD_REJECTED", "reason_zh": reason},
        )
    if watchlist.contains(symbol):
        return {
            "ok": True,
            "added": False,
            "symbol": symbol,
            "watchlist": watchlist.load(),
            "reason_zh": "该交易对已在自选中",
        }
    if len(watchlist.load()) >= MAX_WATCHLIST:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "WATCHLIST_FULL",
                "reason_zh": f"自选最多 {MAX_WATCHLIST} 个，请先移除不用的交易对",
            },
        )
    symbols = watchlist.add(symbol)
    if entry is not None and isinstance(client, MockClient):
        parsed = filters_from_entry(entry)
        if parsed is not None:
            client.set_filters(symbol, parsed)
    oplog.append(
        "watchlist_add",
        symbol=symbol,
        reason_zh=f"已将 {symbol} 加入自选",
    )
    return {
        "ok": True,
        "added": True,
        "symbol": symbol,
        "watchlist": symbols,
        "reason_zh": f"已将 {symbol} 加入自选，现在可以开单",
    }


@app.delete("/api/watchlist/{symbol}")
def watchlist_remove(
    symbol: str,
    _: None = Depends(require_token),
    watchlist: WatchlistStore = Depends(request_watchlist),
    oplog: OpLog = Depends(request_oplog),
    client: BinanceClient = Depends(request_client),
):
    target = normalize_symbol(symbol)
    if not watchlist.contains(target):
        return {
            "ok": True,
            "removed": False,
            "symbol": target,
            "watchlist": watchlist.load(),
            "reason_zh": "该交易对不在自选中",
        }
    symbols = watchlist.remove(target)
    still_open = []
    try:
        still_open = [
            p
            for p in client.get_positions()
            if normalize_symbol(str(p.get("symbol") or "")) == target
        ]
    except Exception:
        still_open = []
    note = f"已将 {target} 移出自选"
    if still_open:
        note += "。该交易对仍有持仓，将继续显示并保留已有保护，但不可再新开仓。"
    oplog.append(
        "watchlist_remove",
        symbol=target,
        reason_zh=note,
        open_positions=len(still_open),
    )
    return {
        "ok": True,
        "removed": True,
        "symbol": target,
        "watchlist": symbols,
        "open_positions": len(still_open),
        "reason_zh": note,
    }


@app.get("/api/indicator")
def indicator(
    source: str = Query(...),
    interval: Optional[str] = Query(default="15m"),
    side: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(require_token),
    settings: Settings = Depends(request_settings),
    client: BinanceClient = Depends(request_client),
    watchlist: WatchlistStore = Depends(request_watchlist),
):
    current = _resolve_symbol(symbol, settings)
    interval_n = normalize_interval(interval) or ""
    if interval_n not in {item[0] for item in INTERVALS}:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "INVALID_INTERVAL",
                "reason_zh": "周期无效，只能是 15min、1h、4h 或 1日",
            },
        )
    source_n = normalize_source(source)
    if source_n is None:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "INVALID_SOURCE",
                "reason_zh": "指标无效，只能是均线或 Supertrend",
            },
        )
    side_n = _normalize_side(side)
    if side_n == "__INVALID__":
        raise HTTPException(
            status_code=400,
            detail={"reason_code": "INVALID_SIDE", "reason_zh": "方向无效，只能是做多或做空"},
        )
    # Watchlist is not a risk override here, but the trading context is the
    # currently selected watchlist symbol. Still compute if asked; opening
    # remains gated elsewhere.
    _ = watchlist  # reserved: callers usually pass the selected symbol
    return _evaluate_indicator_fill(
        settings=settings,
        client=client,
        symbol=current,
        interval=interval_n,
        source=source_n,
        side=side_n,
    )



@app.get("/api/macd")
def macd(
    interval: Optional[str] = Query(default="15m"),
    symbol: Optional[str] = Query(default=None),
    _: None = Depends(require_token),
    settings: Settings = Depends(request_settings),
    client: BinanceClient = Depends(request_client),
):
    """Read-only MACD golden/death cross and smaller-TF divergence. Never used to open."""
    current = _resolve_symbol(symbol, settings)
    interval_n = normalize_interval(interval) or ""
    if interval_n not in {item[0] for item in INTERVALS}:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "INVALID_INTERVAL",
                "reason_zh": "周期无效，只能是 15min、1h、4h 或 1日",
            },
        )
    return _evaluate_macd(
        settings=settings,
        client=client,
        symbol=current,
        interval=interval_n,
    )


@app.get("/api/server-info")
def server_info(settings: Settings = Depends(get_settings)):
    egress = egress_ip()
    return {
        "live_allowed": settings.live_allowed,
        "egress_ip": egress.get("ip"),
        "egress_error": egress.get("error"),
        "testnet_base": TESTNET_FAPI,
        "live_base": LIVE_FAPI,
        "key_storage_zh": (
            "API Key/Secret 只保存在你自己的浏览器里（localStorage）。需要签名时经 HTTPS "
            "随请求发到本服务器，服务器只在这一次请求的内存里用它签名后转发给币安，"
            "不写磁盘、不写日志、不回传 Secret。"
        ),
        "live_enable_zh": "实盘需要运行服务的人在服务端设置 ALLOW_LIVE=yes 并重启；网页上无法打开。",
        "region": _region_payload(settings),
        **_storage_payload(settings),
    }


@app.post("/api/account/verify")
def account_verify(ctx: RequestContext = Depends(resolve_context)):
    """Signed read-only check of the caller's own key. Never places orders."""
    if ctx.mode == "mock":
        return {"ok": True, "mode": "mock", "reason_zh": "模拟模式不使用 API Key"}
    client = ctx.client
    try:
        info = client.verify() if isinstance(client, SignedClient) else {}
    except ClientError as exc:
        ctx.oplog.append(
            "api_verify",
            ok=False,
            mode=ctx.mode,
            reason_code=exc.code,
            reason_zh="API 验证失败",
        )
        return {
            "ok": False,
            "mode": ctx.mode,
            "key_masked": mask_key(ctx.api_key),
            "reason_code": exc.code,
            "binance_message": exc.message,
            "reason_zh": explain_binance_error(exc.code, exc.message),
        }
    ctx.oplog.append("api_verify", ok=True, mode=ctx.mode, reason_zh="API 验证通过（只读查询余额）")
    return {
        "ok": True,
        "mode": ctx.mode,
        "key_masked": mask_key(ctx.api_key),
        "usdt_balance": info.get("usdt_balance"),
        "reason_zh": "验证通过：已用签名只读接口读到合约账户余额",
    }


@app.post("/api/preview")
def preview(
    body: OpenRequest,
    _: None = Depends(require_token),
    settings: Settings = Depends(request_settings),
    client: BinanceClient = Depends(request_client),
    watchlist: WatchlistStore = Depends(request_watchlist),
    oplog: OpLog = Depends(request_oplog),
):
    try:
        snap = _snapshot(
            settings,
            client,
            watchlist,
            oplog,
            stop=body.stop,
            side=body.side,
            symbol=body.symbol,
        )
    except ClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"reason_code": exc.code, "reason_zh": exc.message},
        ) from exc
    decision = snap.get("preview")
    if decision is None:
        current = snap["symbol"]
        gate = require_in_watchlist(current, watchlist.load())
        if gate is not None:
            decision = gate.to_dict()
        else:
            decision = evaluate(
                side=body.side,
                stop=body.stop,
                mark=Decimal(snap["mark"]),
                balance=Decimal(snap["balance"]),
                filters=client.get_filters(current),
            ).to_dict()
    if decision.get("status") == "REJECT":
        oplog.append(
            "reject",
            event_detail="preview",
            symbol=snap["symbol"],
            side=decision.get("side") or body.side,
            stop=decision.get("stop") or (None if body.stop is None else str(body.stop)),
            reason_code=decision.get("reason_code"),
            reason_zh=decision.get("reason_zh"),
        )
    else:
        oplog.append(
            "preview",
            symbol=snap["symbol"],
            side=decision.get("side"),
            stop=decision.get("stop"),
            quantity=decision.get("quantity"),
            risk_usd=decision.get("risk_usd"),
            reason_zh="预览通过",
        )
    return {**snap, "preview": decision}


def _open_position(
    body: OpenRequest,
    settings: Settings,
    client: BinanceClient,
    watchlist: WatchlistStore,
    oplog: OpLog,
) -> dict:
    current = _resolve_symbol(body.symbol, settings)
    # Fresh mark at send time; re-run the same hard gates, including watchlist.
    mark = client.get_mark_price(current)
    balance, balance_field = client.get_balance()
    filters = client.get_filters(current)
    gate = require_in_watchlist(current, watchlist.load())
    decision = gate if gate is not None else evaluate(
        side=body.side,
        stop=body.stop,
        mark=mark,
        balance=balance,
        filters=filters,
    )
    base = {
        "mode": settings.mode,
        "symbol": current,
        "balance": str(balance),
        "balance_field": balance_field,
        "mark": str(mark),
        "decision": decision.to_dict(),
        "positions": _safe_positions(client),
        "has_unprotected": False,
        "logs": oplog.read(limit=50),
    }
    if not decision.passed:
        oplog.append(
            "reject",
            event_detail="open",
            symbol=current,
            side=decision.side or body.side,
            stop=None if decision.stop is None else str(decision.stop),
            reason_code=decision.reason_code,
            reason_zh=decision.reason_zh,
        )
        base["logs"] = oplog.read(limit=50)
        base["has_unprotected"] = any(not p.get("protected") for p in base["positions"])
        return {**base, "status": "REJECT", "opened": False}

    client.ensure_account_settings(current)

    try:
        entry_order = client.market_open(
            current, decision.side or "", decision.quantity or Decimal("0")
        )
    except ClientError as exc:
        reason = f"开仓下单失败：{exc.message}"
        oplog.append(
            "reject",
            event_detail="open",
            symbol=current,
            side=decision.side,
            stop=None if decision.stop is None else str(decision.stop),
            reason_code=exc.code,
            reason_zh=reason,
        )
        base["logs"] = oplog.read(limit=50)
        return {
            **base,
            "status": "REJECT",
            "opened": False,
            "reason_code": exc.code,
            "reason_zh": reason,
        }

    entry = fill_price(entry_order, decision.mark or mark)
    entry_id = str(entry_order.get("orderId", ""))
    oplog.append(
        "open",
        symbol=current,
        side=decision.side,
        quantity=None if decision.quantity is None else str(decision.quantity),
        stop=None if decision.stop is None else str(decision.stop),
        entry_price=str(entry),
        entry_order_id=entry_id,
        reason_zh="开仓已成交",
    )

    try:
        stop_order = client.place_stop(
            current,
            decision.side or "",
            decision.stop or Decimal("0"),
            decision.quantity or Decimal("0"),
        )
    except ClientError as exc:
        flatten_result = None
        flatten_ok = False
        flatten_error = None
        try:
            flatten_result = client.flatten(
                current,
                decision.side or "",
                decision.quantity or Decimal("0"),
            )
            flatten_ok = True
        except ClientError as flat_exc:
            flatten_error = flat_exc.message

        status = "unprotected"
        if flatten_ok:
            status = "flattened"
        if isinstance(client, MockClient):
            client.record_position(
                symbol=current,
                side=decision.side or "",
                quantity=decision.quantity or Decimal("0"),
                entry=entry,
                stop=decision.stop or Decimal("0"),
                notional=decision.notional or Decimal("0"),
                risk_usd=decision.risk_usd or Decimal("0"),
                entry_order_id=entry_id,
                stop_order_id=None,
                status=status,
            )

        oplog.append(
            "stop_failed",
            symbol=current,
            side=decision.side,
            entry_order_id=entry_id,
            reason_zh=f"保护性止损挂单失败：{exc.message}",
            flatten_ok=flatten_ok,
        )
        if flatten_ok:
            oplog.append(
                "flatten",
                symbol=current,
                side=decision.side,
                entry_order_id=entry_id,
                reason_zh="止损失败后已按失败路径市价平掉新仓",
                ok=True,
            )
            reason = (
                "开仓已成交，但止损单失败，已按失败路径市价平掉新仓，仓位不视为受保护。"
                f" 止损错误：{exc.message}"
            )
            code = "STOP_FAILED_FLATTENED"
        else:
            oplog.append(
                "flatten",
                symbol=current,
                side=decision.side,
                entry_order_id=entry_id,
                reason_zh=f"止损失败后平仓也失败：{flatten_error}",
                ok=False,
            )
            reason = (
                "开仓已成交，但止损单失败，随后平仓也失败。仓位目前未受保护，请立即人工处理。"
                f" 止损错误：{exc.message}；平仓错误：{flatten_error}"
            )
            code = "STOP_FAILED_UNPROTECTED"
        positions = _safe_positions(client)
        return {
            **base,
            "status": "REJECT",
            "opened": False,
            "unprotected": not flatten_ok,
            "reason_code": code,
            "reason_zh": reason,
            "entry_order": _public_order(entry_order),
            "flatten_order": _public_order(flatten_result) if flatten_result else None,
            "positions": positions,
            "has_unprotected": any(not p.get("protected") for p in positions),
            "logs": oplog.read(limit=50),
        }

    stop_id = str(stop_order.get("orderId", ""))
    oplog.append(
        "stop_placed",
        symbol=current,
        side=decision.side,
        stop=None if decision.stop is None else str(decision.stop),
        quantity=None if decision.quantity is None else str(decision.quantity),
        entry_order_id=entry_id,
        stop_order_id=stop_id,
        reason_zh="保护性止损已挂上（reduce-only）",
    )
    position = None
    if isinstance(client, MockClient):
        position = client.record_position(
            symbol=current,
            side=decision.side or "",
            quantity=decision.quantity or Decimal("0"),
            entry=entry,
            stop=decision.stop or Decimal("0"),
            notional=decision.notional or Decimal("0"),
            risk_usd=decision.risk_usd or Decimal("0"),
            entry_order_id=entry_id,
            stop_order_id=stop_id,
            status="open",
        )

    positions = _safe_positions(client)
    return {
        **base,
        "status": "PASS",
        "opened": True,
        "reason_code": "PASS",
        "reason_zh": "开仓成功，保护性止损已挂上",
        "entry_order_id": entry_id,
        "stop_order_id": stop_id,
        "entry_order": _public_order(entry_order),
        "stop_order": _public_order(stop_order),
        "position": position,
        "positions": positions,
        "has_unprotected": any(not p.get("protected") for p in positions),
        "logs": oplog.read(limit=50),
    }


def _public_order(order: Optional[dict]) -> Optional[dict]:
    if not order:
        return None
    keys = (
        "orderId",
        "symbol",
        "side",
        "type",
        "status",
        "avgPrice",
        "executedQty",
        "cumQuote",
        "stopPrice",
        "origQty",
        "reduceOnly",
    )
    return {k: order[k] for k in keys if k in order}


@app.post("/api/open")
def open_trade(
    body: OpenRequest,
    _: None = Depends(require_token),
    settings: Settings = Depends(request_settings),
    client: BinanceClient = Depends(request_client),
    watchlist: WatchlistStore = Depends(request_watchlist),
    oplog: OpLog = Depends(request_oplog),
):
    try:
        return _open_position(body, settings, client, watchlist, oplog)
    except ClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"reason_code": exc.code, "reason_zh": exc.message},
        ) from exc


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
