"""Unit tests for MACD golden/death cross and smaller-timeframe divergence."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.binance_client import ClientError, MockClient
from app.config import Settings
from app.engine import SymbolFilters
from app.indicators import Candle, closed_candles
from app.macd import (
    LOWER_INTERVAL,
    MacdPoint,
    analyze_macd,
    compute_macd_points,
    detect_cross,
    detect_divergence,
    ema_series,
    lower_interval,
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
    if closed:
        close_time = NOW_MS - (200 - index) * INTERVAL_MS - 1
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


def candle(index: int, high, low, close=None, open_=None) -> Candle:
    high_d = Decimal(str(high))
    low_d = Decimal(str(low))
    close_d = Decimal(str(close)) if close is not None else (high_d + low_d) / 2
    open_d = Decimal(str(open_)) if open_ is not None else close_d
    close_time = NOW_MS - (200 - index) * INTERVAL_MS - 1
    open_time = close_time - INTERVAL_MS + 1
    return Candle(
        open_time=open_time,
        close_time=close_time,
        open=open_d,
        high=high_d,
        low=low_d,
        close=close_d,
    )


def pt(dif, dea) -> MacdPoint:
    d = Decimal(str(dif))
    e = Decimal(str(dea))
    return MacdPoint(dif=d, dea=e, hist=d - e)


def test_lower_interval_mapping():
    assert lower_interval("1d") == "4h"
    assert lower_interval("4h") == "1h"
    assert lower_interval("1h") == "15m"
    assert lower_interval("15m") == "5m"
    assert lower_interval("5m") is None
    assert LOWER_INTERVAL["15m"] == "5m"
    # Trading intervals all have a smaller TF; 5m is not a trading choice.
    assert set(LOWER_INTERVAL) == {"1d", "4h", "1h", "15m"}


def test_ema_series_sma_seed():
    values = [Decimal(i) for i in (1, 2, 3, 4, 5)]
    out = ema_series(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == Decimal("2")  # SMA(1,2,3)
    # k = 2/4 = 0.5; 4*0.5 + 2*0.5 = 3
    assert out[3] == Decimal("3")
    # 5*0.5 + 3*0.5 = 4
    assert out[4] == Decimal("4")
    assert ema_series(values, 6) == [None] * 5


def test_golden_cross_most_recent_and_bars_ago():
    points = [
        pt(-2, -1),
        pt(-1, -0.5),
        pt(0.5, 0.0),  # 金叉：-1 <= -0.5 且 0.5 > 0
        pt(0.6, 0.2),
    ]
    result = detect_cross(points)
    assert result["ok"] is True
    assert result["type"] == "golden"
    assert result["type_zh"] == "金叉"
    assert result["bias"] == "LONG"
    assert result["bars_ago"] == 1
    assert result["dif_vs_dea"] == "above"
    assert result["dif_vs_zero"] == "above"

    just_now = detect_cross([pt(-1, -0.5), pt(0.5, 0.0)])
    assert just_now["type"] == "golden"
    assert just_now["bars_ago"] == 0


def test_death_cross_beats_older_golden():
    points = [
        pt(-1, -0.5),
        pt(0.5, 0.0),  # 金叉
        pt(0.4, 0.1),
        pt(-0.2, 0.0),  # 更近的死叉
    ]
    result = detect_cross(points)
    assert result["ok"] is True
    assert result["type"] == "death"
    assert result["type_zh"] == "死叉"
    assert result["bias"] == "SHORT"
    assert result["bars_ago"] == 0
    assert result["dif_vs_dea"] == "below"
    assert result["dif_vs_zero"] == "below"


def test_no_cross_when_dif_stays_on_one_side():
    points = [pt(-2, -0.5), pt(-1, -0.4), pt(-0.3, 0.0)]
    result = detect_cross(points)
    assert result["ok"] is True
    assert result["type"] is None
    assert result["type_zh"] == "无交叉"
    assert result["bias"] is None
    assert result["dif_vs_dea"] == "below"
    assert "尚未出现" in result["reason_zh"]


def test_cross_skips_warmup_nones():
    points = [None, None, pt(-1, 0), pt(0.5, 0.1)]
    result = detect_cross(points)
    assert result["type"] == "golden"
    assert result["bars_ago"] == 0

    empty = detect_cross([None, None])
    assert empty["ok"] is False
    assert "不足" in empty["reason_zh"]


def _red(dif, high, low, index):
    # hist > 0 when dif > dea; pick dea = dif - 1
    return candle(index, high, low, close=high), pt(dif, Decimal(str(dif)) - 1)


def _green(dif, high, low, index):
    return candle(index, high, low, close=low), pt(dif, Decimal(str(dif)) + 1)


def test_bearish_divergence_higher_high_lower_max_dif():
    items = []
    # 前一段红柱：高点 102，DIF 最大 5
    for i, (h, d) in enumerate(((100, 3), (101, 4), (102, 5)), start=0):
        items.append(_red(d, h, 90, i))
    # 间隔绿柱
    items.append(_green(-1, 95, 88, 3))
    items.append(_green(-2, 94, 87, 4))
    # 最近红柱：高点 105 > 102，DIF 最大 4 < 5 → 顶背驰
    for i, (h, d) in enumerate(((103, 2), (104, 3), (105, 4)), start=5):
        items.append(_red(d, h, 91, i))
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["ok"] is True
    assert result["type"] == "bearish"
    assert result["type_zh"] == "顶背驰"
    assert result["bias"] == "SHORT"
    assert result["metric"] == "dif_extremum"
    assert result["last_price_high"] == "105"
    assert result["prev_price_high"] == "102"
    assert result["last_max_dif"] == "4"
    assert result["prev_max_dif"] == "5"


def test_bullish_divergence_lower_low_higher_min_dif():
    items = []
    # 前一段绿柱：低点 98，DIF 最小 -5
    for i, (lo, d) in enumerate(((100, -3), (99, -4), (98, -5)), start=0):
        items.append(_green(d, 110, lo, i))
    items.append(_red(1, 112, 105, 3))
    items.append(_red(2, 113, 106, 4))
    # 最近绿柱：低点 95 < 98，DIF 最小 -2 > -5 → 底背驰
    for i, (lo, d) in enumerate(((97, -1), (96, -2), (95, -1.5)), start=5):
        items.append(_green(d, 108, lo, i))
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["ok"] is True
    assert result["type"] == "bullish"
    assert result["type_zh"] == "底背驰"
    assert result["bias"] == "LONG"
    assert result["last_price_low"] == "95"
    assert result["prev_price_low"] == "98"
    assert result["last_min_dif"] == "-2"
    assert result["prev_min_dif"] == "-5"


def test_no_divergence_when_price_and_dif_agree():
    items = []
    items.append(_red(3, 100, 90, 0))
    items.append(_green(-1, 95, 88, 1))
    # 更高高点且更高 DIF：不是顶背驰
    items.append(_red(5, 110, 91, 2))
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["ok"] is True
    assert result["type"] is None
    assert result["type_zh"] == "无背驰"
    assert "不构成顶背驰" in result["reason_zh"]


def test_no_divergence_without_two_same_sign_segments():
    items = [_red(3, 100, 90, 0), _red(4, 101, 91, 1), _green(-1, 95, 88, 2)]
    # 最后一段是绿柱，窗口内没有前一段绿柱
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["type"] is None
    assert result["type_zh"] == "无背驰"
    assert "不足两段" in result["reason_zh"]


def test_no_divergence_on_equal_highs():
    items = [
        _red(5, 100, 90, 0),
        _green(-1, 95, 88, 1),
        _red(3, 100, 91, 2),  # 高点相等，即使 DIF 更低也不算
    ]
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["type"] is None
    assert result["type_zh"] == "无背驰"


def test_divergence_lookback_ignores_older_segments():
    items = []
    # 很早的两段红柱本可以构成顶背驰，但会落在 lookback 之外
    items.append(_red(9, 100, 90, 0))
    items.append(_green(-1, 95, 88, 1))
    items.append(_red(1, 120, 91, 2))
    items.append(_green(-2, 96, 89, 3))
    # 窗口内只有最近一段绿柱
    candles, points = zip(*items)
    full = detect_divergence(list(candles), list(points), lookback=100)
    # last is green and only one green in full window after the last red... 
    # actually last is green at index 3, previous green at 1 → not 底背驰 (lows 89 vs 88, 89 is not lower)
    assert full["type"] is None
    windowed = detect_divergence(list(candles), list(points), lookback=1)
    assert windowed["type"] is None
    assert "不足两段" in windowed["reason_zh"]


def test_zero_histogram_breaks_segments():
    # Two red groups split by hist=0 should still be two red segments → 顶背驰 possible
    items = [
        (candle(0, 100, 90, 95), pt(5, 4)),  # hist +1
        (candle(1, 101, 90, 96), MacdPoint(Decimal("5"), Decimal("5"), Decimal("0"))),
        (candle(2, 110, 91, 100), pt(3, 2)),  # hist +1, higher high, lower max DIF
    ]
    candles, points = zip(*items)
    result = detect_divergence(list(candles), list(points), lookback=100)
    assert result["type"] == "bearish"
    assert result["type_zh"] == "顶背驰"


def test_compute_macd_warmup_and_constant_series():
    closes = [Decimal("100")] * 50
    points = compute_macd_points(closes, 12, 26, 9)
    # First DIF at index 25, first DEA at 25+8=33
    assert all(p is None for p in points[:33])
    assert points[33] is not None
    last = points[-1]
    assert last is not None
    assert last.dif == Decimal("0")
    assert last.dea == Decimal("0")
    assert last.hist == Decimal("0")
    cross = detect_cross(points)
    assert cross["type"] is None


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


def _closed_rows(n=60, price="100000"):
    # Use timestamps far in the past so closed_candles(now) keeps them.
    return [kline(i, price, price, price, price, closed=True) for i in range(n)]


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
    rows = _closed_rows()
    client.set_klines("BTCUSDT", "15m", rows)
    client.set_klines("BTCUSDT", "5m", rows)
    client.set_klines("BTCUSDT", "1h", rows)
    client.set_klines("BTCUSDT", "4h", rows)
    client.set_klines("BTCUSDT", "1d", rows)
    main._client = client
    with TestClient(main.app) as tc:
        yield tc, client, settings
    main.reset_singletons()


def test_macd_endpoint_constant_series_has_no_cross_or_divergence(harness):
    tc, client, settings = harness
    resp = tc.get("/api/macd", params={"symbol": "BTCUSDT", "interval": "15m"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reference_only"] is True
    assert body["note_zh"] == "仅供参考，不影响开单"
    assert body["interval"] == "15m"
    assert body["lower_interval"] == "5m"
    assert body["params_label"] == "MACD(12,26,9)"
    assert body["cross"]["type"] is None
    assert body["cross"]["type_zh"] == "无交叉"
    assert body["cross"]["label_zh"] == "15m MACD：无交叉"
    assert body["divergence"]["type"] is None
    assert body["divergence"]["type_zh"] == "无背驰"
    assert body["divergence"]["label_zh"] == "5m 小级别：无背驰"
    assert body["divergence"]["metric"] == "dif_extremum"


def test_macd_endpoint_maps_each_trading_interval(harness):
    tc, client, settings = harness
    expected = {"15m": "5m", "1h": "15m", "4h": "1h", "1d": "4h"}
    for interval, lower in expected.items():
        resp = tc.get("/api/macd", params={"symbol": "BTCUSDT", "interval": interval})
        assert resp.status_code == 200
        body = resp.json()
        assert body["interval"] == interval
        assert body["lower_interval"] == lower


def test_macd_endpoint_rejects_invalid_interval(harness):
    tc, client, settings = harness
    resp = tc.get("/api/macd", params={"symbol": "BTCUSDT", "interval": "5m"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "INVALID_INTERVAL"


def test_macd_endpoint_network_failure_is_reference_only(harness):
    tc, client, settings = harness

    def boom(symbol, interval, limit=200):
        raise ClientError("网络不可用", code="NETWORK")

    client.get_klines = boom  # type: ignore[method-assign]
    resp = tc.get("/api/macd", params={"symbol": "BTCUSDT", "interval": "15m"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "获取失败" in body["reason_zh"]
    assert body["cross"]["label_zh"] == "15m MACD：获取失败"
    assert body["divergence"]["label_zh"] == "5m 小级别：获取失败"
    assert body["reference_only"] is True


def test_open_and_preview_ignore_macd(harness):
    tc, client, settings = harness
    missing = tc.post("/api/open", json={"side": "LONG", "symbol": "BTCUSDT"})
    assert missing.status_code == 200
    body = missing.json()
    assert body["status"] == "REJECT"
    assert body["opened"] is False
    assert body["decision"]["reason_code"] == "MISSING_STOP"
    assert "macd" not in body
    assert "cross" not in body

    preview = tc.post(
        "/api/preview",
        json={"side": "LONG", "stop": "90000", "symbol": "BTCUSDT"},
    )
    assert preview.status_code == 200
    pdata = preview.json()
    assert pdata["preview"]["status"] == "PASS"
    assert "cross" not in (pdata.get("preview") or {})
    # 开仓闸门不读取 MACD：有止损且规则满足即通过，与 MACD 无交叉无关
    opened = tc.post(
        "/api/open",
        json={"side": "LONG", "stop": "90000", "symbol": "BTCUSDT"},
    )
    assert opened.status_code == 200
    ob = opened.json()
    assert ob["status"] == "PASS"
    assert ob["opened"] is True


def test_meta_exposes_macd_params_as_reference(harness):
    tc, client, settings = harness
    resp = tc.get("/api/meta")
    assert resp.status_code == 200
    macd = resp.json()["macd"]
    assert macd["params_label"] == "MACD(12,26,9)"
    assert macd["reference_only"] is True
    assert macd["lower_intervals"]["15m"] == "5m"


def test_analyze_macd_labels():
    # Enough constant candles for a valid MACD with no cross / no divergence
    rows = [kline(i, "10", "10", "10", "10") for i in range(50)]
    candles = closed_candles(rows, now_ms=NOW_MS + 10_000_000)
    report = analyze_macd(
        interval_candles=candles,
        lower_candles=candles,
        interval="1h",
        lower="15m",
        fast=12,
        slow=26,
        signal=9,
        lookback=100,
        symbol="BTCUSDT",
    )
    assert report["cross"]["label_zh"] == "1h MACD：无交叉"
    assert report["divergence"]["label_zh"] == "15m 小级别：无背驰"
    assert report["note_zh"] == "仅供参考，不影响开单"
