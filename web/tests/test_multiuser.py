"""Multi-user public link: per-browser isolation, per-request keys, abuse limits."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.binance_client import ClientError, MockClient, SignedClient
from app.config import Settings
from app.engine import SymbolFilters

BTC = SymbolFilters(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("100"),
)

CID_A = "clientAAAAAAAAAAAAAAAAAA"
CID_B = "clientBBBBBBBBBBBBBBBBBB"
KEY = "K" * 20 + "abcd1234"
SECRET = "S3cr3tValue" + "Z" * 40


def _settings(tmp_path: Path, **extra) -> Settings:
    base = dict(
        mode="mock",
        symbol="BTCUSDT",
        ledger_path=tmp_path / "ledger.json",
        watchlist_path=tmp_path / "watchlist.json",
        oplog_path=tmp_path / "ops.jsonl",
        clients_dir=tmp_path / "clients",
        mock_balance=1000,
        allow_live="no",
    )
    base.update(extra)
    return Settings(**base)


def _prep_mock(settings: Settings, cid: str) -> MockClient:
    client = main.mock_client_for(cid, settings)
    client.set_mark("BTCUSDT", Decimal("100000"))
    client.set_filters("BTCUSDT", BTC)
    client.set_mark("ETHUSDT", Decimal("3000"))
    client.set_filters("ETHUSDT", BTC)
    return client


def _h(cid: str, mode: str = "mock", key: str | None = None, secret: str | None = None) -> dict:
    h = {"X-Client-Id": cid, "X-Bn-Mode": mode}
    if key:
        h["X-Bn-Key"] = key
    if secret:
        h["X-Bn-Secret"] = secret
    return h


@pytest.fixture
def harness(tmp_path):
    settings = _settings(tmp_path)
    main.reset_singletons()
    main._settings = settings
    legacy = MockClient(settings)
    legacy.set_mark("BTCUSDT", Decimal("100000"))
    legacy.set_filters("BTCUSDT", BTC)
    main._client = legacy
    a = _prep_mock(settings, CID_A)
    b = _prep_mock(settings, CID_B)
    with TestClient(main.app) as tc:
        yield tc, settings, a, b
    main.reset_singletons()


def _fake_validate(symbol, **kwargs):
    return True, "ok", {
        "symbol": str(symbol).upper(),
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [],
    }


def test_two_clients_are_isolated(harness, monkeypatch):
    tc, settings, a, b = harness
    monkeypatch.setattr("app.main.validate_usdtm_perp", _fake_validate)

    assert tc.post("/api/watchlist", json={"symbol": "ETHUSDT"}, headers=_h(CID_A)).status_code == 200
    wa = tc.get("/api/watchlist", headers=_h(CID_A)).json()["watchlist"]
    wb = tc.get("/api/watchlist", headers=_h(CID_B)).json()["watchlist"]
    assert "ETHUSDT" in wa
    assert "ETHUSDT" not in wb

    opened = tc.post(
        "/api/open", json={"side": "LONG", "stop": "99000", "symbol": "BTCUSDT"}, headers=_h(CID_A)
    ).json()
    assert opened["status"] == "PASS"
    assert opened["decision"]["quantity"] == "0.05"

    sa = tc.get("/api/state", headers=_h(CID_A)).json()
    sb = tc.get("/api/state", headers=_h(CID_B)).json()
    assert len(sa["positions"]) == 1
    assert sb["positions"] == []
    assert any(item["event"] == "open" for item in sa["logs"])
    assert not any(item["event"] == "open" for item in sb["logs"])
    # Separate files per client, and the legacy ledger untouched.
    assert (settings.clients_dir / CID_A / "ledger.json").exists()
    assert not (settings.clients_dir / CID_B / "ledger.json").exists()
    assert not Path(settings.ledger_path).exists()
    # New client starts at the default mock balance.
    assert Decimal(sb["balance"]) == Decimal("1000")


def test_watcher_sweeps_every_client_ledger(harness):
    tc, settings, a, b = harness
    tc.post("/api/open", json={"side": "LONG", "stop": "99000", "symbol": "BTCUSDT"}, headers=_h(CID_A))
    tc.post("/api/open", json={"side": "SHORT", "stop": "101000", "symbol": "BTCUSDT"}, headers=_h(CID_B))
    a.set_mark("BTCUSDT", Decimal("98900"))
    b.set_mark("BTCUSDT", Decimal("101100"))
    closed = main.sweep_mock_stops()
    assert len(closed) == 2
    assert tc.get("/api/positions", headers=_h(CID_A)).json()["positions"] == []
    assert tc.get("/api/positions", headers=_h(CID_B)).json()["positions"] == []
    logs_a = tc.get("/api/logs", headers=_h(CID_A)).json()["logs"]
    assert logs_a[0]["event"] == "stop_triggered"


@pytest.mark.parametrize("bad", ["../../etc", "short", "a/b" * 10, "x" * 65, "..%2F..%2Fpasswd0000"])
def test_client_id_rejects_traversal(harness, bad):
    tc, settings, a, b = harness
    resp = tc.get("/api/state", headers={"X-Client-Id": bad})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "INVALID_CLIENT_ID"


def test_live_still_requires_allow_live(harness):
    tc, settings, a, b = harness
    resp = tc.get("/api/state", headers=_h(CID_A, "live", KEY, SECRET))
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason_code"] == "LIVE_DISABLED"
    resp = tc.post("/api/open", json={"side": "LONG", "stop": "99000"}, headers=_h(CID_A, "live", KEY, SECRET))
    assert resp.status_code == 403


def test_signed_mode_requires_keys(harness):
    tc, settings, a, b = harness
    resp = tc.get("/api/state", headers=_h(CID_A, "testnet"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "API_KEY_REQUIRED"


def _fake_signed(monkeypatch, *, fail: ClientError | None = None):
    seen = []

    def fake_request(self, method, path, *, params=None, signed=False):
        seen.append((self._key, self._secret, path))
        if fail is not None and path == "/fapi/v2/balance":
            raise fail
        if path == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "321.5"}]
        if path == "/fapi/v1/premiumIndex":
            return {"markPrice": "100000"}
        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "100"},
                        ],
                    }
                ]
            }
        if path == "/fapi/v2/account":
            return {"totalMarginBalance": "321.5"}
        if path in {"/fapi/v2/positionRisk", "/fapi/v1/openOrders"}:
            return []
        raise AssertionError(f"unexpected call {path}")

    monkeypatch.setattr(SignedClient, "_request", fake_request)
    return seen


def _all_files_text(root: Path) -> str:
    out = []
    for path in root.rglob("*"):
        if path.is_file():
            out.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(out)


def test_secret_used_per_request_never_stored_or_returned(harness, monkeypatch, caplog, tmp_path):
    tc, settings, a, b = harness
    seen = _fake_signed(monkeypatch)
    caplog.set_level(logging.DEBUG)
    headers = _h(CID_A, "testnet", KEY, SECRET)

    verify = tc.post("/api/account/verify", headers=headers)
    assert verify.status_code == 200
    body = verify.json()
    assert body["ok"] is True
    assert body["usdt_balance"] == "321.5"
    assert body["key_masked"] == f"{KEY[:4]}…{KEY[-4:]}"

    state = tc.get("/api/state", headers=headers)
    assert state.status_code == 200
    assert state.json()["mode"] == "testnet"
    assert Decimal(state.json()["balance"]) == Decimal("321.5")

    # The browser's own key was used to sign.
    assert seen and all(k == KEY and s == SECRET for k, s, _ in seen)
    for resp in (verify, state):
        assert SECRET not in resp.text
        assert KEY not in resp.text
    disk = _all_files_text(tmp_path)
    assert SECRET not in disk
    assert KEY not in disk
    assert SECRET not in caplog.text
    assert KEY not in caplog.text


def test_verify_failure_explains_binance_error(harness, monkeypatch):
    tc, settings, a, b = harness
    _fake_signed(
        monkeypatch,
        fail=ClientError(
            "币安接口错误 (-2015): Invalid API-key, IP, or permissions for action.", code="-2015"
        ),
    )
    body = tc.post("/api/account/verify", headers=_h(CID_A, "testnet", KEY, SECRET)).json()
    assert body["ok"] is False
    assert body["reason_code"] == "-2015"
    assert "白名单" in body["reason_zh"]
    assert SECRET not in str(body)


def test_risk_gates_unchanged_per_client(harness):
    tc, settings, a, b = harness
    h = _h(CID_B)
    no_stop = tc.post("/api/open", json={"side": "LONG", "symbol": "BTCUSDT"}, headers=h).json()
    assert no_stop["status"] == "REJECT"
    not_listed = tc.post(
        "/api/open", json={"side": "LONG", "stop": "2900", "symbol": "ETHUSDT"}, headers=h
    ).json()
    assert not_listed["decision"]["reason_code"] == "NOT_IN_WATCHLIST"
    ok = tc.post("/api/open", json={"side": "SHORT", "stop": "101000", "symbol": "BTCUSDT"}, headers=h).json()
    assert ok["status"] == "PASS"
    assert ok["decision"]["risk_usd"] in {"50", "50.0", "50.00"} or Decimal(ok["decision"]["risk_usd"]) == Decimal("50")
    state = tc.get("/api/state", headers=h).json()
    assert state["leverage"] == 20
    assert state["margin_mode_zh"] == "全仓"
    assert state["risk_fraction"] == "0.05"


def test_rate_limit_per_ip(tmp_path):
    settings = _settings(tmp_path, rate_limit_per_minute=5)
    main.reset_singletons()
    main._settings = settings
    _prep_mock(settings, CID_A)
    try:
        with TestClient(main.app) as tc:
            codes = [tc.get("/api/logs", headers=_h(CID_A)).status_code for _ in range(7)]
            assert codes[:5] == [200] * 5
            assert codes[5] == 429
            # Different (Cloudflare-forwarded) IP has its own budget.
            other = {**_h(CID_A), "CF-Connecting-IP": "203.0.113.9"}
            assert tc.get("/api/logs", headers=other).status_code == 200
    finally:
        main.reset_singletons()


def test_client_capacity_limit(tmp_path):
    settings = _settings(tmp_path, max_clients=1)
    main.reset_singletons()
    main._settings = settings
    _prep_mock(settings, CID_A)
    try:
        with TestClient(main.app) as tc:
            assert tc.get("/api/logs", headers=_h(CID_A)).status_code == 200
            resp = tc.get("/api/logs", headers=_h("clientCCCCCCCCCCCCCCCCCC"))
            assert resp.status_code == 503
    finally:
        main.reset_singletons()


def test_watchlist_cap(harness, monkeypatch):
    tc, settings, a, b = harness
    monkeypatch.setattr("app.main.validate_usdtm_perp", _fake_validate)
    monkeypatch.setattr("app.main.MAX_WATCHLIST", 2)
    assert tc.post("/api/watchlist", json={"symbol": "ETHUSDT"}, headers=_h(CID_A)).status_code == 200
    resp = tc.post("/api/watchlist", json={"symbol": "SOLUSDT"}, headers=_h(CID_A))
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "WATCHLIST_FULL"


def test_oplog_is_size_bounded(tmp_path, monkeypatch):
    import app.oplog as oplog_mod

    monkeypatch.setattr(oplog_mod, "MAX_BYTES", 2000)
    monkeypatch.setattr(oplog_mod, "KEEP_LINES", 5)
    log = oplog_mod.OpLog(tmp_path / "ops.jsonl")
    for i in range(200):
        log.append("preview", reason_zh=f"第 {i} 条")
    assert (tmp_path / "ops.jsonl").stat().st_size < 4000
