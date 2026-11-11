"""Abuse limits for the public link: per-IP rate limit, client_id validation,
and per-request Binance credentials taken from headers (never stored).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Optional

import httpx

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
API_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")

HEADER_CLIENT_ID = "x-client-id"
HEADER_MODE = "x-bn-mode"
HEADER_KEY = "x-bn-key"
HEADER_SECRET = "x-bn-secret"
SENSITIVE_HEADERS = {HEADER_KEY, HEADER_SECRET, "x-app-token", "authorization", "cookie"}

WRITE_PATHS = ("/api/open", "/api/watchlist", "/api/account/verify")


def valid_client_id(value: Optional[str]) -> bool:
    return bool(value) and bool(CLIENT_ID_RE.match(value or ""))


def valid_api_credential(value: Optional[str]) -> bool:
    return bool(value) and bool(API_KEY_RE.match(value or ""))


def mask_key(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    key = str(key)
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def client_ip(request, trust_proxy: bool = False) -> str:
    """Client IP for rate limiting.

    Forwarded headers are trusted only when the peer is local (a tunnel) or when
    TRUST_PROXY=yes (behind a hosting platform's proxy, e.g. Render). They can be
    spoofed by a client, so they are used for rate limiting only, never for auth.
    """
    peer = request.client.host if request.client else "unknown"
    if trust_proxy or peer in {"127.0.0.1", "::1", "localhost", "testclient"}:
        for name in ("cf-connecting-ip", "true-client-ip"):
            value = request.headers.get(name)
            if value:
                return value.strip()[:64]
        forwarded = request.headers.get("cf-connecting-ip")
        if forwarded:
            return forwarded.strip()[:64]
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return peer


class RateLimiter:
    """Sliding one-minute window per (bucket, ip)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def allow(self, bucket: str, ip: str, limit: int, now: Optional[float] = None) -> bool:
        if limit <= 0:
            return True
        now = time.time() if now is None else now
        key = (bucket, ip)
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) > 50000:
                    self._hits.clear()
                hits = self._hits[key] = deque()
            while hits and now - hits[0] >= 60.0:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


BINANCE_ERROR_HINTS: dict[str, str] = {
    "-2015": (
        "API Key 无效，或服务器出口 IP 不在该 Key 的白名单里，或没有勾选「允许合约」。"
        "也可能把测试网 Key 用在了实盘（或反过来）。"
    ),
    "-2014": "API Key 格式不正确，请检查是否复制完整、没有多余空格。",
    "-2008": "API Key 不存在（可能已删除，或环境选错：测试网与实盘 Key 不通用）。",
    "-1022": "签名无效：API Secret 不正确。",
    "-1021": "时间戳超出范围：服务器时间与币安相差过大。",
    "-1003": "请求过于频繁，被币安限速（所有用户共用服务器出口 IP），请稍后再试。",
    "-1002": "未授权：该 Key 没有访问此接口的权限。",
    "451": "地区限制：币安拒绝来自服务器所在地区的访问（restricted location）。",
    "403": "访问被拒（可能是地区限制或币安防火墙拦截）。",
    "418": "服务器出口 IP 被币安临时封禁（请求过多），请稍后再试。",
    "429": "请求过于频繁，被币安限速。",
    "NETWORK": "服务器连不上币安接口，请稍后重试。",
}


def explain_binance_error(code: Optional[str], message: str) -> str:
    text = (message or "").lower()
    if "restricted location" in text or "eligibility" in text:
        return BINANCE_ERROR_HINTS["451"]
    return BINANCE_ERROR_HINTS.get(
        str(code or ""), "币安返回错误，请对照错误码检查 Key、权限和 IP 白名单。"
    )


_egress_cache: dict[str, Any] = {"ip": None, "at": 0.0}
EGRESS_TTL = 600.0


def egress_ip() -> dict[str, Any]:
    now = time.time()
    if _egress_cache["ip"] and now - _egress_cache["at"] < EGRESS_TTL:
        return {"ip": _egress_cache["ip"], "error": None}
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get("https://api.ipify.org", params={"format": "json"})
            resp.raise_for_status()
            ip = str(resp.json().get("ip") or "").strip()
        if ip:
            _egress_cache.update(ip=ip, at=now)
            return {"ip": ip, "error": None}
    except Exception as exc:  # pragma: no cover - network dependent
        return {"ip": _egress_cache["ip"], "error": f"获取出口 IP 失败：{type(exc).__name__}"}
    return {"ip": None, "error": "获取出口 IP 失败"}
