"""Startup self-check: can this server's egress IP reach Binance USD-M futures?

Two probes against the official mainnet base (no API key is ever sent):

1. Public  GET /fapi/v1/time
2. Signed  GET /fapi/v2/balance with no key and a dummy signature.
   From an eligible location Binance answers 400/401 with an API-key error
   (e.g. -2014 / -2015 / -1102). From a restricted location it answers 451
   ("Service unavailable from a restricted location ..."); a 403 means a
   WAF / CloudFront block. Either of those is shown as a red banner.

The result is informational only. It never changes risk rules or order logic.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import httpx

PROBE_BASE = "https://fapi.binance.com"
PUBLIC_PATH = "/fapi/v1/time"
SIGNED_PATH = "/fapi/v2/balance"
PROBE_TIMEOUT = 8.0
RECHECK_SECONDS = 1800.0

BLOCK_STATUSES = {451: "REGION_451", 403: "FORBIDDEN_403"}

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "pending",  # pending | ok | blocked | error
    "checked_at": None,
    "public": None,
    "signed": None,
    "message_zh": "正在检测本服务器能否访问币安合约接口…",
}

Getter = Callable[[str, dict, dict], tuple[int, str]]


def _http_get(url: str, params: dict, headers: dict) -> tuple[int, str]:
    with httpx.Client(timeout=PROBE_TIMEOUT) as client:
        resp = client.get(url, params=params, headers=headers)
    return resp.status_code, resp.text[:300]


def _one(name: str, path: str, params: dict, getter: Getter) -> dict[str, Any]:
    try:
        status, body = getter(f"{PROBE_BASE}{path}", params, {})
    except Exception as exc:  # network dependent
        return {"name": name, "http_status": None, "kind": "network", "detail": type(exc).__name__}
    if status in BLOCK_STATUSES:
        kind = "blocked"
    elif name == "public":
        kind = "ok" if status == 200 else "unexpected"
    else:
        # Without a key, an eligible location gets an auth error, not data.
        kind = "ok" if status in (400, 401) else ("unexpected" if status != 200 else "ok")
    return {"name": name, "http_status": status, "kind": kind, "detail": body[:200]}


def classify(public: dict, signed: dict) -> tuple[str, str]:
    probes = (public, signed)
    blocked = [p for p in probes if p["kind"] == "blocked"]
    if blocked:
        codes = "、".join(f"{p['name']}={p['http_status']}" for p in blocked)
        if any(p["http_status"] == 451 for p in blocked):
            why = "币安返回 451（受限地区）"
        else:
            why = "币安返回 403（防火墙/地区拦截）"
        return (
            "blocked",
            f"本服务器所在地区无法使用币安合约接口：{why}，{codes}。"
            "模拟模式的行情和指标也可能读不到，测试网/实盘下单会失败。请换部署地区（例如 Frankfurt）。",
        )
    if any(p["kind"] == "network" for p in probes):
        return "error", "无法连接币安合约接口（网络错误），稍后会自动重试。"
    if all(p["kind"] == "ok" for p in probes):
        return "ok", "本服务器可以访问币安合约公开接口和签名接口（未发送任何 Key）。"
    codes = "、".join(f"{p['name']}={p['http_status']}" for p in probes)
    return "error", f"币安接口返回异常状态（{codes}），稍后会自动重试。"


def run_probe(getter: Optional[Getter] = None) -> dict[str, Any]:
    getter = getter or _http_get
    public = _one("public", PUBLIC_PATH, {}, getter)
    signed = _one(
        "signed",
        SIGNED_PATH,
        {"timestamp": str(int(time.time() * 1000)), "signature": "0" * 64},
        getter,
    )
    status, message = classify(public, signed)
    result = {
        "status": status,
        "checked_at": int(time.time()),
        "public": public,
        "signed": signed,
        "message_zh": message,
    }
    with _lock:
        _state.update(result)
    return dict(result)


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def reset_for_tests() -> None:
    with _lock:
        _state.update(status="pending", checked_at=None, public=None, signed=None,
                      message_zh="正在检测本服务器能否访问币安合约接口…")
