"""用 Python 原版（/workspace/binance-open-controller/app）生成对拍输入与期望输出。

python3 Tools/parity_python.py <python项目路径> fixture.json py_out.json
"""
import json, random, sys, time, hmac, hashlib
from decimal import Decimal
import urllib.request

sys.path.insert(0, sys.argv[1])
from app.engine import evaluate, SymbolFilters, _decimal_str  # noqa: E402
from app.indicators import (closed_candles, compute_sma, compute_supertrend, suggest_stop,  # noqa: E402
                            candle_label, ma_params_label, supertrend_params_label)
from app.macd import analyze_macd, lower_interval  # noqa: E402

random.seed(20261110)
FAPI = "https://fapi.binance.com/fapi/v1/klines"


def klines(symbol, interval, limit):
    url = f"{FAPI}?symbol={symbol}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def synth(n, start, vol, interval_ms, seed, decimals):
    rnd = random.Random(seed)
    rows, price, t = [], start, 1_700_000_000_000
    for _ in range(n):
        o = price
        c = max(o * (1 + rnd.gauss(0, vol)), 10 ** -decimals)
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        f = lambda x: f"{x:.{decimals}f}"
        rows.append([t, f(o), f(h), f(l), f(c), "1", t + interval_ms - 1])
        price, t = c, t + interval_ms
    return rows


fixture = {"engine": [], "suggest": [], "series": [], "sign": []}

# ---------- 真实 K 线（同一份数据给两边） ----------
now_ms = int(time.time() * 1000)
for sym in ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "1000PEPEUSDT"]:
    for iv in ["15m", "1h", "4h", "1d"]:
        low = lower_interval(iv)
        fixture["series"].append({"name": f"real-{sym}-{iv}", "interval": iv, "lower": low, "now_ms": now_ms,
                                  "rows": klines(sym, iv, 200), "lower_rows": klines(sym, low, 200)})

# ---------- 合成 K 线（多种价格量级、短序列边界） ----------
for k in range(40):
    dec = random.choice([1, 2, 4, 6, 8])
    start = random.choice([95000.0, 3500.0, 2.5, 0.15, 0.0000123])
    n = random.choice([5, 9, 10, 19, 20, 34, 35, 60, 200, 200, 200])
    rows = synth(n, start, random.choice([0.002, 0.01, 0.03]), 900_000, 1000 + k, dec)
    low = synth(min(n * 3, 300), start, 0.01, 300_000, 5000 + k, dec)
    fixture["series"].append({"name": f"synth-{k}", "interval": "15m", "lower": "5m", "now_ms": 10 ** 13,
                              "rows": rows, "lower_rows": low})
# 含正在形成的 K 线：now 落在最后一根之内
r = synth(50, 100.0, 0.01, 900_000, 77, 2)
fixture["series"].append({"name": "forming-candle", "interval": "1h", "lower": "15m", "now_ms": r[-1][0] + 10,
                          "rows": r, "lower_rows": r})

# ---------- 风控引擎 ----------
FILTERS = [
    {"tick": "0.1", "step": "0.001", "min_qty": "0.001", "min_notional": "100"},
    {"tick": "0.01", "step": "0.01", "min_qty": "0.01", "min_notional": "20"},
    {"tick": "0.00001", "step": "1", "min_qty": "1", "min_notional": "5"},
    {"tick": "0.0000001", "step": "1", "min_qty": "1", "min_notional": "5"},
]
for k in range(600):
    f = random.choice(FILTERS)
    mark = Decimal(str(round(random.uniform(0.00001, 120000), random.choice([1, 2, 5, 7]))))
    if mark <= 0:
        mark = Decimal("1")
    side = random.choice(["LONG", "SHORT", "BUY", "sell", "", None, "up"])
    off = Decimal(str(round(random.uniform(-0.2, 0.2), 6)))
    stop = mark * (1 + off)
    stop_s = random.choice([str(stop), _decimal_str(stop), "", None, "abc", "-5", "0", str(mark)])
    bal = random.choice(["1000", "0", "12.34", "100000", "-1", "", "abc", "57.123456"])
    fixture["engine"].append({"side": side, "stop": stop_s, "mark": str(mark), "balance": bal, "filters": f})
# 有效单（大部分应 PASS，覆盖向下取整/最小量/最小名义金额边界）
for k in range(500):
    f = random.choice(FILTERS)
    mark = Decimal(str(round(random.uniform(0.001, 120000), random.choice([2, 3, 5]))))
    if mark <= 0:
        mark = Decimal("0.5")
    side = random.choice(["LONG", "SHORT"])
    pct = Decimal(str(round(random.uniform(0.0005, 0.12), 6)))
    stop = mark * (1 - pct) if side == "LONG" else mark * (1 + pct)
    bal = str(Decimal(str(round(random.uniform(5, 50000), 4))))
    fixture["engine"].append({"side": side, "stop": _decimal_str(stop), "mark": str(mark), "balance": bal, "filters": f})
fixture["engine"].append({"side": "LONG", "stop": "99", "mark": "100", "balance": "1000", "filters": {"tick": "0", "step": "1", "min_qty": "1", "min_notional": "1"}})

# ---------- 止损自动填入 ----------
for k in range(300):
    mark = Decimal(str(round(random.uniform(0.001, 100000), 4)))
    v = mark * (1 + Decimal(str(round(random.uniform(-0.05, 0.05), 6))))
    if k % 17 == 0:
        v = mark
    tick = random.choice(["0.1", "0.01", "0.0001", None])
    fixture["suggest"].append({"interval": random.choice(["15m", "1h", "4h", "1d"]), "source": random.choice(["ma", "supertrend"]),
                               "value": str(v), "mark": str(mark), "side": random.choice(["LONG", "SHORT", None]), "tick": tick,
                               "params_label": random.choice(["", "SMA20", "ATR10 × 3"])})

# ---------- 签名 ----------
fixture["sign"].append({"secret": "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
                        "message": "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"})
for k in range(40):
    sec = "".join(random.choice("abcdefXYZ0123456789") for _ in range(random.choice([1, 63, 64, 65, 128])))
    msg = f"symbol=BTCUSDT&side=SELL&type=STOP_MARKET&quantity=0.0{k}&timestamp={1700000000000 + k}"
    fixture["sign"].append({"secret": sec, "message": msg})

# ---------- Python 期望输出 ----------
def s(x):
    return None if x is None else _decimal_str(x)

out = {"engine": [], "suggest": [], "series": [], "sign": []}
for c in fixture["engine"]:
    f = c["filters"]
    flt = SymbolFilters.from_raw(f["tick"], f["step"], f["min_qty"], f["min_notional"])
    d = evaluate(side=c["side"], stop=c["stop"], mark=c["mark"], balance=c["balance"], filters=flt)
    out["engine"].append({"status": d.status, "reason_code": d.reason_code, "reason_zh": d.reason_zh, "side": d.side,
                          "stop": s(d.stop), "quantity": s(d.quantity), "raw_quantity": s(d.raw_quantity),
                          "notional": s(d.notional), "risk_usd": s(d.risk_usd)})
for c in fixture["suggest"]:
    fill = suggest_stop(interval=c["interval"], source=c["source"], value=Decimal(c["value"]), mark=Decimal(c["mark"]),
                        side=c["side"], tick_size=None if c["tick"] is None else Decimal(c["tick"]), params_label=c["params_label"])
    out["suggest"].append({"fill": s(fill.fill), "usable": fill.usable_for_side, "reason": fill.reason})
for c in fixture["series"]:
    candles = closed_candles(c["rows"], now_ms=c["now_ms"])
    lower = closed_candles(c["lower_rows"], now_ms=c["now_ms"])
    sma = compute_sma(candles, 20)
    st = compute_supertrend(candles, 10, Decimal("3"))
    m = analyze_macd(interval_candles=candles, lower_candles=lower, interval=c["interval"], lower=c["lower"],
                     fast=12, slow=26, signal=9, lookback=100)
    cr, dv = m["cross"], m["divergence"]
    out["series"].append({"name": c["name"], "n_closed": len(candles), "sma": s(sma.value), "sma_reason": sma.reason_zh,
                          "supertrend": s(st.value), "st_reason": st.reason_zh, "candle_label": candle_label(c["interval"], sma.candle),
                          "cross": {k: cr.get(k) for k in ["type", "type_zh", "bars_ago", "dif", "dea", "histogram", "reason_zh", "label_zh"]},
                          "divergence": {k: dv.get(k) for k in ["type", "type_zh", "reason_zh", "label_zh", "last_max_dif", "last_min_dif",
                                                                "prev_max_dif", "prev_min_dif", "last_price_high", "prev_price_high",
                                                                "last_price_low", "prev_price_low"]},
                          "macd_ok": m["ok"], "macd_reason": m["reason_zh"]})
for c in fixture["sign"]:
    out["sign"].append({"sig": hmac.new(c["secret"].encode(), c["message"].encode(), hashlib.sha256).hexdigest()})

json.dump(fixture, open(sys.argv[2], "w"), ensure_ascii=False)
json.dump(out, open(sys.argv[3], "w"), ensure_ascii=False, indent=1, sort_keys=True)
print("fixture:", {k: len(v) for k, v in fixture.items()})
