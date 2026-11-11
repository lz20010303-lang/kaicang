"""Unit tests for SMA20, Supertrend, and indicator stop auto-fill rules."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.binance_client import MockClient
from app.config import Settings
from app.engine import SymbolFilters, align_stop
from app.indicators import (
    SOURCE_MA,
    SOURCE_SUPERTREND,
    closed_candles,
    compute_sma,
    compute_supertrend,
    parse_candle,
    sma_from_closes,
    suggest_stop,
)
from app.watchlist import WatchlistStore

BTC = SymbolFilters(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("100"),
)

NOW_MS = 1_700_000_000_000
INTERVAL_MS = 15 * 60 * 1000


def kline(index: int, open_: str, high: str, low: str, close: str, *, closed: bool = True):
    """Build a Binance-style kline. Closed candles end before NOW_MS."""
    if closed:
        close_time = NOW_MS - (20 - index) * INTERVAL_MS - 1
        open_time = close_time - INTERVAL_MS + 1
    else:
        open_time = NOW_MS - 1
        close_time = NOW_MS + INTERVAL_MS - 1
    return [
        open_time,
        str(open_),
        str(high),
        str(low),
        str(close),
        "1",
        close_time,
        "1",
        1,
        "0",
        "0",
        "0",
    ]


def candles_from(rows, now_ms=NOW_MS):
    return closed_candles(rows, now_ms=now_ms)


def test_sma20_on_known_close_series():
    closes = [Decimal(i) for i in range(1, 21)]
    assert sma_from_closes(closes, 20) == Decimal("10.5")

    # Sliding window uses only the last 20 values.
    closes = [Decimal("0")] + [Decimal(i) for i in range(1, 21)]
    assert sma_from_closes(closes, 20) == Decimal("10.5")

    rows = [
        kline(i, str(i), str(i), str(i), str(i), closed=True)
        for i in range(1, 21)
    ]
    computed = compute_sma(candles_from(rows), 20)
    assert computed.ok
    assert computed.value == Decimal("10.5")


def test_sma20_short_series_does_not_invent_value():
    closes = [Decimal(i) for i in range(1, 20)]
    assert sma_from_closes(closes, 20) is None
    rows = [kline(i, "1", "1", "1", "1") for i in range(19)]
    computed = compute_sma(candles_from(rows), 20)
    assert computed.value is None
    assert computed.ok is False
    assert "不足" in (computed.reason_zh or "")


def _hand_supertrend_fixture():
    """Period 3, multiplier 2. Hand-calculated last Supertrend = 16.

    TR: 2, 2, 2, 2, 5
    ATR3: n/a, n/a, 2, 2, 3
    i=2: hl2=12, up=8, down=16, trend=1, st=8
    i=3: hl2=13, up=9, down=17 → trend_up=9, trend_down=16, trend=1, st=9
    i=4: hl2=10, up=4, down=16 → trend_up=9, trend_down=16, close 8.5 < 9,
         trend=-1, st=16
    """
    rows = [
        kline(0, "10", "11", "9", "10"),
        kline(1, "10", "12", "10", "11"),
        kline(2, "11", "13", "11", "12"),
        kline(3, "12", "14", "12", "13"),
        kline(4, "13", "12", "8", "8.5"),
    ]
    return rows


def test_supertrend_on_small_fixture():
    rows = _hand_supertrend_fixture()
    computed = compute_supertrend(candles_from(rows), 3, Decimal("2"))
    assert computed.ok
    assert computed.value == Decimal("16")


def test_supertrend_default_params_need_enough_closed_candles():
    rows = [kline(i, "10", "11", "9", "10") for i in range(9)]
    computed = compute_supertrend(candles_from(rows), 10, Decimal("3"))
    assert computed.value is None
    assert "不足" in (computed.reason_zh or "")


def test_long_only_accepts_indicator_below_mark():
    mark = Decimal("100000")
    tick = Decimal("0.1")
    ok = suggest_stop(
        interval="15m",
        source=SOURCE_MA,
        value=Decimal("99000.04"),
        mark=mark,
        side="LONG",
        tick_size=tick,
        params_label="SMA20",
    )
    assert ok.usable_for_side is True
    assert ok.fill == align_stop(Decimal("99000.04"), tick, "LONG")
    assert ok.fill == Decimal("99000.0")
    assert ok.fill < mark
    assert "已填入止损" in ok.reason

    # Equal to mark is not strictly below.
    equal = suggest_stop(
        interval="15m",
        source=SOURCE_MA,
        value=mark,
        mark=mark,
        side="LONG",
        tick_size=tick,
        params_label="SMA20",
    )
    assert equal.usable_for_side is False
    assert equal.fill is None


def test_short_only_accepts_indicator_above_mark():
    mark = Decimal("100000")
    tick = Decimal("0.1")
    ok = suggest_stop(
        interval="1h",
        source=SOURCE_SUPERTREND,
        value=Decimal("101000.04"),
        mark=mark,
        side="SHORT",
        tick_size=tick,
        params_label="ATR10 × 3",
    )
    assert ok.usable_for_side is True
    assert ok.fill == align_stop(Decimal("101000.04"), tick, "SHORT")
    assert ok.fill == Decimal("101000.1")
    assert ok.fill > mark
    assert "已填入止损" in ok.reason


def test_wrong_side_does_not_produce_a_fill_value():
    mark = Decimal("100000")
    tick = Decimal("0.1")
    long_wrong = suggest_stop(
        interval="15m",
        source=SOURCE_MA,
        value=Decimal("101000"),
        mark=mark,
        side="LONG",
        tick_size=tick,
        params_label="SMA20",
    )
    assert long_wrong.fill is None
    assert long_wrong.usable_for_side is False
    assert long_wrong.value == Decimal("101000")
    assert "当前周期均线在价格上方，不能作为多头止损" in long_wrong.reason

    short_wrong = suggest_stop(
        interval="15m",
        source=SOURCE_SUPERTREND,
        value=Decimal("99000"),
        mark=mark,
        side="SHORT",
        tick_size=tick,
        params_label="ATR10 × 3",
    )
    assert short_wrong.fill is None
    assert short_wrong.usable_for_side is False
    assert "当前周期 Supertrend 在价格下方，不能作为空头止损" in short_wrong.reason


def test_no_direction_fills_only_line_below_mark():
    mark = Decimal("100000")
    below = suggest_stop(
        interval="4h",
        source=SOURCE_MA,
        value=Decimal("99000"),
        mark=mark,
        side=None,
        tick_size=Decimal("0.1"),
        params_label="SMA20",
    )
    assert below.usable_for_side is True
    assert below.fill == Decimal("99000")

    above = suggest_stop(
        interval="4h",
        source=SOURCE_MA,
        value=Decimal("101000"),
        mark=mark,
        side=None,
        tick_size=Decimal("0.1"),
        params_label="SMA20",
    )
    assert above.fill is None
    assert above.usable_for_side is False
    assert "价格上方" in above.reason
    assert "未选择方向" in above.reason


def test_incomplete_or_missing_klines_do_not_invent_a_value():
    assert closed_candles(None, now_ms=NOW_MS) == []
    assert closed_candles([], now_ms=NOW_MS) == []

    forming = kline(99, "100", "110", "90", "105", closed=False)
    assert forming[6] >= NOW_MS
    assert closed_candles([forming], now_ms=NOW_MS) == []

    # A mixed batch drops the forming candle and does not use its close.
    closed_rows = [kline(i, str(i + 1), str(i + 1), str(i + 1), str(i + 1)) for i in range(20)]
    mixed = closed_rows + [forming]
    candles = closed_candles(mixed, now_ms=NOW_MS)
    assert len(candles) == 20
    assert all(c.close_time < NOW_MS for c in candles)
    computed = compute_sma(candles, 20)
    # Forming close 105 must not enter the window; last closed close is 20.
    assert computed.value == sma_from_closes([Decimal(i + 1) for i in range(20)], 20)

    # Unparsable / non-finite rows are skipped, not replaced with a guess.
    bad = [
        ["x", "1", "1", "1", "1", "1", NOW_MS - 10],
        kline(0, "1", "1", "1", "NaN"),
        [NOW_MS - 100, "1", "2", "0.5", "inf", "1", NOW_MS - 50],
    ]
    assert closed_candles(bad, now_ms=NOW_MS) == []
    assert parse_candle(bad[1]) is None

    short = compute_sma(closed_candles(closed_rows[:5], now_ms=NOW_MS), 20)
    assert short.value is None

    missing_st = compute_supertrend([], 10, 3)
    assert missing_st.value is None


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


@pytest.fixture
def harness(tmp_path):
    settings = _settings(tmp_path)
    store = WatchlistStore(settings.watchlist_path)
    store._save(["BTCUSDT"])
    main.reset_singletons()
    main._settings = settings
    client = MockClient(settings)
    client.set_mark("BTCUSDT", Decimal("100000"))
    client.set_filters("BTCUSDT", BTC)
    # 20 closed closes 1..20, SMA20 = 10.5; plus a forming candle that must be ignored.
    rows = [kline(i, str(i), str(i), str(i), str(i)) for i in range(1, 21)]
    rows.append(kline(99, "200000", "200000", "200000", "200000", closed=False))
    client.set_klines("BTCUSDT", "15m", rows)
    main._client = client
    with TestClient(main.app) as tc:
        yield tc, client, settings
    main.reset_singletons()


def test_indicator_endpoint_fills_long_from_sma_below_mark(harness):
    tc, client, settings = harness
    # Put a realistic MA below the mark: closes around 99000.
    rows = [kline(i, "99000", "99000", "99000", "99000") for i in range(20)]
    client.set_klines("BTCUSDT", "15m", rows)
    resp = tc.get(
        "/api/indicator",
        params={"source": "ma", "interval": "15m", "side": "LONG", "symbol": "BTCUSDT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["interval"] == "15m"
    assert body["source"] == "ma"
    assert body["value"] == "99000"
    assert body["usable_for_side"] is True
    assert body["fill"] == "99000"
    assert "已填入止损" in body["reason"]


def test_indicator_endpoint_wrong_side_has_no_fill(harness):
    tc, client, settings = harness
    rows = [kline(i, "101000", "101000", "101000", "101000") for i in range(20)]
    client.set_klines("BTCUSDT", "15m", rows)
    resp = tc.get(
        "/api/indicator",
        params={"source": "均线", "interval": "15min", "side": "LONG", "symbol": "BTCUSDT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == "101000"
    assert body["fill"] is None
    assert body["usable_for_side"] is False
    assert "不能作为多头止损" in body["reason"]


def test_open_still_rejects_missing_stop_even_with_indicator_context(harness):
    tc, client, settings = harness
    # Opening does not accept a source in place of a numeric stop.
    resp = tc.post("/api/open", json={"side": "LONG", "symbol": "BTCUSDT"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "REJECT"
    assert body["opened"] is False
    assert body["decision"]["reason_code"] == "MISSING_STOP"
