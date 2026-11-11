"""Deterministic MACD cross and smaller-timeframe divergence (reference only).

Computed locally from official Binance USD-M klines. Only last *closed* candles
are used. Results are never fed into the opening / preview risk gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Sequence

from app.engine import _decimal_str
from app.indicators import Candle, INTERVAL_LABELS

# MACD lines are differences of EMAs; keep 8 decimal places then strip zeros.
_MACD_QUANT = Decimal("0.00000001")


def _macd_str(value: Decimal) -> str:
    if value is None:
        return None  # type: ignore[return-value]
    quantized = value.quantize(_MACD_QUANT)
    return _decimal_str(quantized)


MACD_FAST_DEFAULT = 12
MACD_SLOW_DEFAULT = 26
MACD_SIGNAL_DEFAULT = 9
MACD_LOOKBACK_DEFAULT = 100

# Trading interval → one step smaller. 15m uses 5m, which is not a trading TF.
LOWER_INTERVAL = {
    "1d": "4h",
    "4h": "1h",
    "1h": "15m",
    "15m": "5m",
}

_EXTRA_INTERVAL_LABELS = {
    "5m": "5min",
}

CROSS_GOLDEN = "golden"
CROSS_DEATH = "death"
DIV_BULLISH = "bullish"  # 底背驰
DIV_BEARISH = "bearish"  # 顶背驰

# Divergence compares the DIF extremum of adjacent same-sign histogram
# segments (max DIF on red / min DIF on green). Histogram area is not used.
DIVERGENCE_METRIC = "dif_extremum"
DIVERGENCE_METRIC_ZH = "该段 DIF 极值（红柱取 DIF 最大，绿柱取 DIF 最小；不用柱面积）"


def macd_interval_label(interval: str) -> str:
    return INTERVAL_LABELS.get(interval) or _EXTRA_INTERVAL_LABELS.get(interval, interval)


def lower_interval(interval: str) -> Optional[str]:
    return LOWER_INTERVAL.get(interval)


def macd_params_label(fast: int, slow: int, signal: int) -> str:
    return f"MACD({fast},{slow},{signal})"


def _cmp_zero(value: Decimal) -> str:
    if value > 0:
        return "above"
    if value < 0:
        return "below"
    return "equal"


def _cmp_zero_zh(which: str, axis: str) -> str:
    # ASCII names (DEA) get spaces; Chinese names (零轴) do not.
    token = axis if axis == "零轴" else f" {axis} "
    if which == "above":
        return f"在{token}上方"
    if which == "below":
        return f"在{token}下方"
    return f"与{token}相等"


def ema_series(values: Sequence[Decimal], period: int) -> list[Optional[Decimal]]:
    """EMA seeded with the SMA of the first ``period`` values (standard MACD)."""
    out: list[Optional[Decimal]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    out[period - 1] = seed
    k = Decimal(2) / Decimal(period + 1)
    one_minus = Decimal(1) - k
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * one_minus
        out[i] = prev
    return out


@dataclass(frozen=True)
class MacdPoint:
    dif: Decimal
    dea: Decimal
    hist: Decimal


def compute_macd_points(
    closes: Sequence[Decimal],
    fast: int,
    slow: int,
    signal: int,
) -> list[Optional[MacdPoint]]:
    """Return MACD points aligned to ``closes``. Warm-up bars are None.

    DIF = EMA(close, fast) − EMA(close, slow).
    DEA = EMA(DIF, signal), seeded on the first ``signal`` valid DIF values.
    Histogram = DIF − DEA.
    """
    n = len(closes)
    out: list[Optional[MacdPoint]] = [None] * n
    if fast <= 0 or slow <= 0 or signal <= 0 or fast >= slow or n < slow + signal - 1:
        return out
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    dif: list[Optional[Decimal]] = [None] * n
    for i in range(n):
        a, b = ema_fast[i], ema_slow[i]
        if a is None or b is None:
            continue
        dif[i] = a - b

    first = next((i for i, v in enumerate(dif) if v is not None), None)
    if first is None or n - first < signal:
        return out
    # The series is contiguous after ``first`` when inputs are finite closes.
    dea_tail = ema_series([v for v in (dif[i] for i in range(first, n))], signal)
    for offset, dea in enumerate(dea_tail):
        i = first + offset
        if dea is None or dif[i] is None:
            continue
        d = dif[i]
        out[i] = MacdPoint(dif=d, dea=dea, hist=d - dea)
    return out


def compute_macd_points_from_candles(
    candles: Sequence[Candle],
    fast: int,
    slow: int,
    signal: int,
) -> list[Optional[MacdPoint]]:
    return compute_macd_points([c.close for c in candles], fast, slow, signal)


def _cross_type(prev_dif: Decimal, prev_dea: Decimal, dif: Decimal, dea: Decimal) -> Optional[str]:
    if prev_dif <= prev_dea and dif > dea:
        return CROSS_GOLDEN
    if prev_dif >= prev_dea and dif < dea:
        return CROSS_DEATH
    return None


def detect_cross(points: Sequence[Optional[MacdPoint]]) -> dict[str, Any]:
    """Most recent DIF/DEA cross among valid adjacent points.

    金叉：DIF 上穿 DEA（上一根 DIF <= DEA，当前 DIF > DEA）。
    死叉：DIF 下穿 DEA（上一根 DIF >= DEA，当前 DIF < DEA）。
    ``bars_ago`` is 0 when the last closed bar is the cross.
    """
    last_idx: Optional[int] = None
    last_type: Optional[str] = None
    last_valid: Optional[int] = None
    prev_valid: Optional[int] = None
    for i, pt in enumerate(points):
        if pt is None:
            continue
        if prev_valid is not None:
            prev = points[prev_valid]
            if prev is not None:
                kind = _cross_type(prev.dif, prev.dea, pt.dif, pt.dea)
                if kind is not None:
                    last_idx = i
                    last_type = kind
        prev_valid = i
        last_valid = i

    if last_valid is None:
        return {
            "ok": False,
            "type": None,
            "type_zh": "无法判断",
            "bias": None,
            "bars_ago": None,
            "dif": None,
            "dea": None,
            "histogram": None,
            "dif_vs_dea": None,
            "dif_vs_zero": None,
            "reason_zh": "已收盘 K 线不足，无法计算 MACD",
        }

    last = points[last_valid]
    assert last is not None
    dif_vs_dea = _cmp_zero(last.dif - last.dea)
    dif_vs_zero = _cmp_zero(last.dif)
    if last_type is None or last_idx is None:
        return {
            "ok": True,
            "type": None,
            "type_zh": "无交叉",
            "bias": None,
            "bars_ago": None,
            "dif": _macd_str(last.dif),
            "dea": _macd_str(last.dea),
            "histogram": _macd_str(last.hist),
            "dif_vs_dea": dif_vs_dea,
            "dif_vs_zero": dif_vs_zero,
            "reason_zh": (
                f"可见已收盘 K 线内尚未出现 DIF 与 DEA 的交叉；"
                f"当前 DIF {_cmp_zero_zh(dif_vs_dea, 'DEA')}，"
                f"{_cmp_zero_zh(dif_vs_zero, '零轴')}"
            ),
        }

    bars_ago = last_valid - last_idx
    type_zh = "金叉" if last_type == CROSS_GOLDEN else "死叉"
    bias = "LONG" if last_type == CROSS_GOLDEN else "SHORT"
    when = "刚收盘" if bars_ago == 0 else f"{bars_ago}根前"
    return {
        "ok": True,
        "type": last_type,
        "type_zh": type_zh,
        "bias": bias,
        "bars_ago": bars_ago,
        "dif": _macd_str(last.dif),
        "dea": _macd_str(last.dea),
        "histogram": _macd_str(last.hist),
        "dif_vs_dea": dif_vs_dea,
        "dif_vs_zero": dif_vs_zero,
        "reason_zh": (
            f"最近一次交叉为{type_zh}（{when}）；"
            f"当前 DIF {_cmp_zero_zh(dif_vs_dea, 'DEA')}，"
            f"{_cmp_zero_zh(dif_vs_zero, '零轴')}"
        ),
    }


def _hist_sign(hist: Decimal) -> int:
    if hist > 0:
        return 1
    if hist < 0:
        return -1
    return 0


@dataclass
class _Segment:
    sign: int
    start: int
    end: int
    price_high: Decimal
    price_low: Decimal
    max_dif: Decimal
    min_dif: Decimal


def _segments(
    candles: Sequence[Candle],
    points: Sequence[Optional[MacdPoint]],
) -> list[_Segment]:
    segs: list[_Segment] = []
    current: Optional[_Segment] = None
    n = min(len(candles), len(points))
    for i in range(n):
        pt = points[i]
        candle = candles[i]
        if pt is None:
            current = None
            continue
        sign = _hist_sign(pt.hist)
        if sign == 0:
            current = None
            continue
        if current is None or current.sign != sign:
            current = _Segment(
                sign=sign,
                start=i,
                end=i,
                price_high=candle.high,
                price_low=candle.low,
                max_dif=pt.dif,
                min_dif=pt.dif,
            )
            segs.append(current)
        else:
            current.end = i
            if candle.high > current.price_high:
                current.price_high = candle.high
            if candle.low < current.price_low:
                current.price_low = candle.low
            if pt.dif > current.max_dif:
                current.max_dif = pt.dif
            if pt.dif < current.min_dif:
                current.min_dif = pt.dif
    return segs


def detect_divergence(
    candles: Sequence[Candle],
    points: Sequence[Optional[MacdPoint]],
    lookback: int = MACD_LOOKBACK_DEFAULT,
) -> dict[str, Any]:
    """MACD 柱符号分段后的顶/底背驰（只用最近约 ``lookback`` 根已收盘且已有 MACD 的 K 线）。

    规则（确定性，比较指标用该段 DIF 极值，不用柱面积）：
    - 按 histogram 符号分段：红柱段 hist>0，绿柱段 hist<0；hist=0 断开分段。
    - 只比较最后一段（允许仍在延续，即尚未反号）与窗口内前一段同向柱。
    - 顶背驰：最后一段为红柱，其价格高点高于前一段红柱高点，且该段 DIF 最大值
      低于前一段 DIF 最大值。
    - 底背驰：最后一段为绿柱，其价格低点低于前一段绿柱低点，且该段 DIF 最小值
      高于前一段 DIF 最小值。
    - 比较均为严格不等式。不满足或不足两段同向柱时为「无背驰」。
    """
    base = {
        "ok": True,
        "type": None,
        "type_zh": "无背驰",
        "bias": None,
        "metric": DIVERGENCE_METRIC,
        "metric_zh": DIVERGENCE_METRIC_ZH,
        "lookback": lookback,
        "last_price_high": None,
        "last_price_low": None,
        "prev_price_high": None,
        "prev_price_low": None,
        "last_max_dif": None,
        "last_min_dif": None,
        "prev_max_dif": None,
        "prev_min_dif": None,
    }
    if lookback <= 0:
        return {**base, "ok": False, "type_zh": "无法判断", "reason_zh": "背驰回看窗口无效"}
    if len(candles) != len(points):
        # Align by the shorter prefix; should not happen in normal use.
        n = min(len(candles), len(points))
        candles = list(candles)[:n]
        points = list(points)[:n]

    valid_idx = [i for i, pt in enumerate(points) if pt is not None]
    if not valid_idx:
        return {
            **base,
            "ok": False,
            "type_zh": "无法判断",
            "reason_zh": "已收盘 K 线不足，无法计算小级别 MACD",
        }

    window_idx = valid_idx[-lookback:]
    start = window_idx[0]
    win_candles = list(candles[start : window_idx[-1] + 1])
    win_points = list(points[start : window_idx[-1] + 1])
    segs = _segments(win_candles, win_points)
    if not segs:
        return {**base, "reason_zh": "小级别窗口内没有可用的 MACD 柱分段，无背驰"}

    last = segs[-1]
    prev = next((s for s in reversed(segs[:-1]) if s.sign == last.sign), None)
    last_payload = {
        "last_price_high": _decimal_str(last.price_high),
        "last_price_low": _decimal_str(last.price_low),
        "last_max_dif": _macd_str(last.max_dif),
        "last_min_dif": _macd_str(last.min_dif),
    }
    if prev is None:
        kind = "红柱" if last.sign > 0 else "绿柱"
        return {
            **base,
            **last_payload,
            "reason_zh": f"小级别窗口内不足两段同向{kind}，无背驰",
        }

    prev_payload = {
        **last_payload,
        "prev_price_high": _decimal_str(prev.price_high),
        "prev_price_low": _decimal_str(prev.price_low),
        "prev_max_dif": _macd_str(prev.max_dif),
        "prev_min_dif": _macd_str(prev.min_dif),
    }

    if last.sign > 0:
        higher_high = last.price_high > prev.price_high
        lower_dif = last.max_dif < prev.max_dif
        if higher_high and lower_dif:
            return {
                **base,
                **prev_payload,
                "type": DIV_BEARISH,
                "type_zh": "顶背驰",
                "bias": "SHORT",
                "reason_zh": (
                    "最近一段红柱的价格高点高于前一段红柱，"
                    "但该段 DIF 最大值低于前一段，判定为顶背驰"
                ),
            }
        return {
            **base,
            **prev_payload,
            "reason_zh": "最近一段红柱与前一段红柱不构成顶背驰（需更高高点且 DIF 最大值更低）",
        }

    lower_low = last.price_low < prev.price_low
    higher_dif = last.min_dif > prev.min_dif
    if lower_low and higher_dif:
        return {
            **base,
            **prev_payload,
            "type": DIV_BULLISH,
            "type_zh": "底背驰",
            "bias": "LONG",
            "reason_zh": (
                "最近一段绿柱的价格低点低于前一段绿柱，"
                "但该段 DIF 最小值高于前一段，判定为底背驰"
            ),
        }
    return {
        **base,
        **prev_payload,
        "reason_zh": "最近一段绿柱与前一段绿柱不构成底背驰（需更低低点且 DIF 最小值更高）",
    }


def _cross_label(interval: str, cross: dict[str, Any], *, failed: bool = False) -> str:
    prefix = f"{interval} MACD："
    if failed:
        return prefix + "获取失败"
    if not cross.get("ok"):
        return prefix + (cross.get("type_zh") or "无法判断")
    if cross.get("type") is None:
        return prefix + (cross.get("type_zh") or "无交叉")
    bars_ago = cross.get("bars_ago")
    when = "刚收盘" if bars_ago == 0 else f"{bars_ago}根前"
    return prefix + f"{cross.get('type_zh')}（{when}）"


def _div_label(lower: str, divergence: dict[str, Any], *, failed: bool = False) -> str:
    prefix = f"{lower} 小级别："
    if failed:
        return prefix + "获取失败"
    if not divergence.get("ok"):
        return prefix + (divergence.get("type_zh") or "无法判断")
    return prefix + (divergence.get("type_zh") or "无背驰")


def empty_failure(
    *,
    interval: str,
    lower: Optional[str],
    reason_zh: str,
    params_label: str,
    fast: int,
    slow: int,
    signal: int,
    lookback: int,
    symbol: str = "",
    cross_failed: bool = True,
    div_failed: bool = True,
    cross: Optional[dict[str, Any]] = None,
    divergence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    lower_tf = lower or ""
    if cross is None:
        cross = {
            "ok": False,
            "type": None,
            "type_zh": "获取失败",
            "bias": None,
            "bars_ago": None,
            "reason_zh": reason_zh,
        }
    if divergence is None:
        divergence = {
            "ok": False,
            "type": None,
            "type_zh": "获取失败",
            "bias": None,
            "metric": DIVERGENCE_METRIC,
            "reason_zh": reason_zh,
        }
    cross = {**cross, "label_zh": _cross_label(interval, cross, failed=cross_failed)}
    divergence = {
        **divergence,
        "interval": lower_tf,
        "interval_label": macd_interval_label(lower_tf) if lower_tf else "",
        "label_zh": _div_label(lower_tf or "小级别", divergence, failed=div_failed),
    }
    return {
        "ok": False,
        "reference_only": True,
        "note_zh": "仅供参考，不影响开单",
        "symbol": symbol,
        "interval": interval,
        "interval_label": macd_interval_label(interval),
        "lower_interval": lower_tf or None,
        "lower_interval_label": macd_interval_label(lower_tf) if lower_tf else None,
        "params_label": params_label,
        "fast": fast,
        "slow": slow,
        "signal": signal,
        "lookback": lookback,
        "reason_zh": reason_zh,
        "cross": cross,
        "divergence": divergence,
    }


def analyze_macd(
    *,
    interval_candles: Sequence[Candle],
    lower_candles: Sequence[Candle],
    interval: str,
    lower: str,
    fast: int,
    slow: int,
    signal: int,
    lookback: int = MACD_LOOKBACK_DEFAULT,
    symbol: str = "",
) -> dict[str, Any]:
    params_label = macd_params_label(fast, slow, signal)
    points = compute_macd_points_from_candles(interval_candles, fast, slow, signal)
    cross = detect_cross(points)
    cross_failed = not cross.get("ok")
    cross["label_zh"] = _cross_label(interval, cross, failed=cross_failed)

    lower_points = compute_macd_points_from_candles(lower_candles, fast, slow, signal)
    divergence = detect_divergence(lower_candles, lower_points, lookback=lookback)
    div_failed = not divergence.get("ok")
    divergence["interval"] = lower
    divergence["interval_label"] = macd_interval_label(lower)
    divergence["label_zh"] = _div_label(lower, divergence, failed=div_failed)

    ok = (not cross_failed) and (not div_failed)
    reason = "仅供参考，不影响开单"
    if cross_failed and div_failed:
        reason = cross.get("reason_zh") or divergence.get("reason_zh") or "获取失败"
    elif cross_failed:
        reason = cross.get("reason_zh") or "当前周期 MACD 获取失败"
    elif div_failed:
        reason = divergence.get("reason_zh") or "小级别 MACD 获取失败"

    return {
        "ok": ok,
        "reference_only": True,
        "note_zh": "仅供参考，不影响开单",
        "symbol": symbol,
        "interval": interval,
        "interval_label": macd_interval_label(interval),
        "lower_interval": lower,
        "lower_interval_label": macd_interval_label(lower),
        "params_label": params_label,
        "fast": fast,
        "slow": slow,
        "signal": signal,
        "lookback": lookback,
        "reason_zh": reason,
        "cross": cross,
        "divergence": divergence,
    }
