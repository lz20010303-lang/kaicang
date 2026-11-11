"""Watchlist gate, mock stop booking, and position protection tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.binance_client import MockClient
from app.config import Settings
from app.engine import SymbolFilters, require_in_watchlist
from app.oplog import OpLog
from app.watchlist import WatchlistStore, classify_symbol_entry

BTC = SymbolFilters(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("100"),
)

ETH = SymbolFilters(
    tick_size=Decimal("0.01"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("20"),
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="mock",
        symbol="BTCUSDT",
        ledger_path=tmp_path / "ledger.json",
        watchlist_path=tmp_path / "watchlist.json",
        oplog_path=tmp_path / "ops.jsonl",
        mock_balance=1000,
        allow_live="no",
    )


def _seed_watchlist(settings: Settings, symbols: list[str]) -> WatchlistStore:
    store = WatchlistStore(settings.watchlist_path)
    # Write exactly the provided list (may be empty after removes in other tests).
    store._save([s.upper() for s in symbols])
    return store


def _mock_client(settings: Settings) -> MockClient:
    client = MockClient(settings)
    client.set_mark("BTCUSDT", Decimal("100000"))
    client.set_filters("BTCUSDT", BTC)
    client.set_mark("ETHUSDT", Decimal("3000"))
    client.set_filters("ETHUSDT", ETH)
    return client


@pytest.fixture
def harness(tmp_path):
    settings = _settings(tmp_path)
    store = _seed_watchlist(settings, ["BTCUSDT"])
    main.reset_singletons()
    main._settings = settings
    client = _mock_client(settings)
    main._client = client
    with TestClient(main.app) as tc:
        yield tc, client, settings, store
    main.reset_singletons()


def test_require_in_watchlist_rejects_unknown_symbol():
    decision = require_in_watchlist("ETHUSDT", ["BTCUSDT"])
    assert decision is not None
    assert decision.status == "REJECT"
    assert decision.reason_code == "NOT_IN_WATCHLIST"
    assert decision.reason_zh == "该交易对不在自选中，请先加入自选后再开单"


def test_require_in_watchlist_accepts_member_case_insensitive():
    assert require_in_watchlist("btcusdt", ["BTCUSDT"]) is None
    assert require_in_watchlist("BTCUSDT", ["ETHUSDT", "BTCUSDT"]) is None


def test_require_in_watchlist_rejects_empty_or_missing_list():
    assert require_in_watchlist("BTCUSDT", []).reason_code == "NOT_IN_WATCHLIST"
    assert require_in_watchlist("BTCUSDT", None).reason_code == "NOT_IN_WATCHLIST"
    assert require_in_watchlist("", ["BTCUSDT"]).reason_code == "NOT_IN_WATCHLIST"


def test_classify_rejects_non_perp_and_unknown():
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "BTCUSDT_250926",
                "contractType": "CURRENT_QUARTER",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
        ]
    }
    ok, reason, _ = classify_symbol_entry("BTCUSDT", info)
    assert ok is True
    ok, reason, _ = classify_symbol_entry("BTCUSDT_250926", info)
    assert ok is False
    assert "不是 U 本位永续" in reason
    ok, reason, _ = classify_symbol_entry("NOTCOINUSDT", info)
    assert ok is False
    assert "未找到" in reason


def test_api_cannot_open_symbol_not_in_watchlist(harness):
    tc, client, settings, store = harness
    resp = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "2900", "symbol": "ETHUSDT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "REJECT"
    assert body["opened"] is False
    assert body["decision"]["reason_code"] == "NOT_IN_WATCHLIST"
    assert body["decision"]["reason_zh"] == "该交易对不在自选中，请先加入自选后再开单"
    # Rejected tickets must not write a position.
    assert client.get_positions() == []
    ledger = client._load()
    assert ledger.get("positions") in (None, [])


def test_api_can_open_after_adding_to_watchlist(harness, monkeypatch):
    tc, client, settings, store = harness

    def fake_validate(symbol, **kwargs):
        if str(symbol).upper() == "ETHUSDT":
            return True, "ok", {
                "symbol": "ETHUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "filters": [],
            }
        return False, "未找到该交易对，请确认是币安 U 本位永续合约代码", None

    monkeypatch.setattr("app.main.validate_usdtm_perp", fake_validate)

    blocked = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "2900", "symbol": "ETHUSDT"},
    )
    assert blocked.json()["decision"]["reason_code"] == "NOT_IN_WATCHLIST"

    added = tc.post("/api/watchlist", json={"symbol": "ETHUSDT"})
    assert added.status_code == 200
    assert added.json()["added"] is True
    assert "ETHUSDT" in added.json()["watchlist"]

    opened = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "2900", "symbol": "ETHUSDT"},
    )
    assert opened.status_code == 200
    body = opened.json()
    assert body["status"] == "PASS"
    assert body["opened"] is True
    assert body["symbol"] == "ETHUSDT"
    # risk 50 / distance 100 = 0.5
    assert body["decision"]["quantity"] == "0.5"
    positions = body["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ETHUSDT"
    assert positions[0]["protected"] is True
    assert positions[0]["stop_resting"] is True


def test_unprotected_position_is_flagged(harness):
    tc, client, settings, store = harness
    client.record_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.05"),
        entry=Decimal("100000"),
        stop=Decimal("99000"),
        notional=Decimal("5000"),
        risk_usd=Decimal("50"),
        entry_order_id="mock-entry-unprot",
        stop_order_id=None,
        status="unprotected",
    )
    rows = client.get_positions()
    assert len(rows) == 1
    assert rows[0]["protected"] is False
    assert rows[0]["stop_resting"] is False
    assert rows[0]["status"] == "unprotected"

    state = tc.get("/api/state", params={"symbol": "BTCUSDT"})
    assert state.status_code == 200
    payload = state.json()
    assert payload["has_unprotected"] is True
    assert payload["positions"][0]["protected"] is False

    dedicated = tc.get("/api/positions")
    assert dedicated.json()["has_unprotected"] is True


def test_protected_position_is_not_alarmed(harness):
    tc, client, settings, store = harness
    client.record_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.05"),
        entry=Decimal("100000"),
        stop=Decimal("99000"),
        notional=Decimal("5000"),
        risk_usd=Decimal("50"),
        entry_order_id="mock-entry-ok",
        stop_order_id="mock-stop-ok",
        status="open",
    )
    payload = tc.get("/api/positions").json()
    assert payload["has_unprotected"] is False
    assert payload["positions"][0]["protected"] is True
    assert payload["positions"][0]["stop_resting"] is True


def test_mock_mark_crossing_stop_books_close_and_updates_equity(harness):
    tc, client, settings, store = harness
    # Fresh ledger wallet is 1000.
    client.record_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.05"),
        entry=Decimal("100000"),
        stop=Decimal("99000"),
        notional=Decimal("5000"),
        risk_usd=Decimal("50"),
        entry_order_id="mock-entry-trig",
        stop_order_id="mock-stop-trig",
        status="open",
    )
    # Not yet crossed.
    assert client.apply_stop_triggers({"BTCUSDT": Decimal("100000")}) == []
    assert client._load()["positions"][0]["status"] == "open"

    closed = main.process_mock_stops(client, main.get_oplog())
    # Still not crossed at the override mark 100000.
    assert closed == []

    client.set_mark("BTCUSDT", Decimal("98900"))
    closed = main.process_mock_stops(client, main.get_oplog())
    assert len(closed) == 1
    item = closed[0]
    # Fill at the then-current mark; PnL = (98900 - 100000) * 0.05 = -55
    assert item["fill_price"] == "98900"
    assert Decimal(item["realized_pnl"]) == Decimal("-55")
    ledger = client._load()
    assert Decimal(ledger["wallet_balance"]) == Decimal("945")
    assert ledger["positions"][0]["status"] == "stopped"
    assert client.get_positions() == []

    balance, _ = client.get_balance()
    assert balance == Decimal("945")

    state = tc.get("/api/state").json()
    assert Decimal(state["balance"]) == Decimal("945")
    assert state["positions"] == []
    events = [row["event"] for row in state["logs"]]
    assert "stop_triggered" in events
    triggered = next(row for row in state["logs"] if row["event"] == "stop_triggered")
    assert triggered["symbol"] == "BTCUSDT"
    assert "标记价" in triggered["reason_zh"]


def test_unprotected_mock_position_is_not_silently_closed_by_watcher(harness):
    tc, client, settings, store = harness
    client.record_position(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("0.05"),
        entry=Decimal("100000"),
        stop=Decimal("99000"),
        notional=Decimal("5000"),
        risk_usd=Decimal("50"),
        entry_order_id="mock-entry-keep",
        stop_order_id=None,
        status="unprotected",
    )
    client.set_mark("BTCUSDT", Decimal("98000"))
    assert client.apply_stop_triggers({"BTCUSDT": Decimal("98000")}) == []
    rows = client.get_positions()
    assert len(rows) == 1
    assert rows[0]["status"] == "unprotected"
    assert rows[0]["protected"] is False
    # Wallet must not be rewritten as if a stop filled.
    assert Decimal(client._load()["wallet_balance"]) == Decimal("1000")


def test_removed_watchlist_symbol_cannot_open_but_position_remains(harness):
    tc, client, settings, store = harness
    client.record_position(
        symbol="BTCUSDT",
        side="SHORT",
        quantity=Decimal("0.05"),
        entry=Decimal("100000"),
        stop=Decimal("101000"),
        notional=Decimal("5000"),
        risk_usd=Decimal("50"),
        entry_order_id="mock-entry-stay",
        stop_order_id="mock-stop-stay",
        status="open",
    )
    removed = tc.delete("/api/watchlist/BTCUSDT")
    assert removed.status_code == 200
    assert "BTCUSDT" not in removed.json()["watchlist"]
    assert removed.json()["open_positions"] == 1

    positions = tc.get("/api/positions").json()["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSDT"
    assert positions[0]["protected"] is True

    blocked = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "99000", "symbol": "BTCUSDT"},
    )
    assert blocked.json()["decision"]["reason_code"] == "NOT_IN_WATCHLIST"
    # Existing position still there, not flattened by the rejected new open.
    assert len(client.get_positions()) == 1


def test_preview_and_open_write_ops_log(harness):
    tc, client, settings, store = harness
    preview = tc.post(
        "/api/preview",
        json={"side": "LONG", "stop": "99000", "symbol": "BTCUSDT"},
    )
    assert preview.status_code == 200
    assert preview.json()["preview"]["status"] == "PASS"

    rejected = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "101000", "symbol": "BTCUSDT"},
    )
    assert rejected.json()["status"] == "REJECT"

    logs = tc.get("/api/logs").json()["logs"]
    events = [row["event"] for row in logs]
    assert "preview" in events
    assert "reject" in events
    # Newest first.
    assert logs[0]["event"] in events
    assert "ts" in logs[0]
    assert "+08:00" in logs[0]["ts"]
    blob = Path(settings.oplog_path).read_text(encoding="utf-8")
    assert "api_key" not in blob.lower()
    assert "secret" not in blob.lower()
