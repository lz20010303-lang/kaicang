"""Deterministic MA / Supertrend helpers computed from Binance USD-M klines.

Binance has no official indicator endpoint. Callers fetch official klines and
pass them here. Only last *closed* candles are used; forming candles and
missing/short history never invent a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence, Union

from app.engine import EngineInputError, _decimal_str, _to_decimal, align_stop

SHANGHAI = timezone(timedelta(hours=8))

INTERVALS: tuple[tuple[str, str], ...] = (
    ("15m", "15min"),
    ("1h", "1h"),
    ("4h", "4h"),
    ("1d", "1日"),
)
ALLOWED_INTERVALS = {item[0] for item in INTERVALS}
INTERVAL_LABELS = {item[0]: item[1] for item in INTERVALS}

_INTERVAL_ALIASES = {
    "15m": "15m",
    "15min": "15m",
    "15分钟": "15m",
    "1h": "1h",
    "60m": "1h",
    "1hour": "1h",
    "1小时": "1h",
    "4h": "4h",
    "4hour": "4h",
    "4小时": "4h",
    "1d": "1d",
    "1day": "1d",
    "1日": "1d",
    "日": "1d",
}

SOURCE_MA = "ma"
SOURCE_SUPERTREND = "supertrend"
_SOURCE_ALIASES = {
    "ma": SOURCE_MA,
    "sma": SOURCE_MA,
    "均线": SOURCE_MA,
    "supertrend": SOURCE_SUPERTREND,
    "supertrading": SOURCE_SUPERTREND,
    "st": SOURCE_SUPERTREND,
    "超级趋势": SOURCE_SUPERTREND,
}


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class IndicatorCompute:
    value: Optional[Decimal]
    candle: Optional[Candle]
    reason_zh: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.value is not None and self.value.is_finite()


@dataclass(frozen=True)
class StopFill:
    interval: str
    source: str
    value: Optional[Decimal]
    fill: Optional[Decimal]
    usable_for_side: bool
    reason: str
    candle: Optional[Candle] = None
    mark: Optional[Decimal] = None
    params_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "interval_label": interval_label(self.interval),
            "source": self.source,
            "source_label": source_label(self.source),
            "params_label": self.params_label,
            "value": None if self.value is None else _decimal_str(self.value),
            "fill": None if self.fill is None else _decimal_str(self.fill),
            "aligned_value": None if self.fill is None else _decimal_str(self.fill),
            "usable_for_side": self.usable_for_side,
            "reason": self.reason,
            "reason_zh": self.reason,
            "mark": None if self.mark is None else _decimal_str(self.mark),
            "candle_open_time": None if self.candle is None else self.candle.open_time,
            "candle_close_time": None if self.candle is None else self.candle.close_time,
            "candle_label": candle_label(self.interval, self.candle),
        }


def normalize_interval(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    # Preserve the Chinese 日 which lowercases to itself.
    key = str(value).strip()
    lowered = key.lower()
    return _INTERVAL_ALIASES.get(lowered) or _INTERVAL_ALIASES.get(key)


def normalize_source(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    return _SOURCE_ALIASES.get(key) or _SOURCE_ALIASES.get(str(value).strip())


def interval_label(interval: str) -> str:
    return INTERVAL_LABELS.get(interval, interval)


def source_label(source: str) -> str:
    if source == SOURCE_MA:
        return "均线"
    if source == SOURCE_SUPERTREND:
        return "Supertrend"
    return source


def ma_params_label(period: int) -> str:
    return f"SMA{period}"


def supertrend_params_label(period: int, multiplier: Union[int, float, Decimal, str]) -> str:
    return f"ATR{period} × {_decimal_str(Decimal(str(multiplier)))}"


def candle_label(interval: str, candle: Optional[Candle]) -> Optional[str]:
    if candle is None:
        return None
    close = datetime.fromtimestamp(candle.close_time / 1000, tz=SHANGHAI)
    stamp = close.strftime("%Y-%m-%d %H:%M")
    return f"{interval_label(interval)} 已收盘 K 线，收盘 {stamp}（Asia/Shanghai）"


def _as_ms(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def parse_candle(row: Any) -> Optional[Candle]:
    """Parse a Binance kline list or a dict with OHLC fields."""
    try:
        if isinstance(row, dict):
            open_time = _as_ms(row.get("open_time", row.get("openTime", 0)))
            close_time = _as_ms(row.get("close_time", row.get("closeTime", 0)))
            open_ = _to_decimal(row.get("open"), allow_missing=False)
            high = _to_decimal(row.get("high"), allow_missing=False)
            low = _to_decimal(row.get("low"), allow_missing=False)
            close = _to_decimal(row.get("close"), allow_missing=False)
        elif isinstance(row, (list, tuple)) and len(row) >= 7:
            open_time = _as_ms(row[0])
            close_time = _as_ms(row[6])
            open_ = _to_decimal(row[1], allow_missing=False)
            high = _to_decimal(row[2], allow_missing=False)
            low = _to_decimal(row[3], allow_missing=False)
            close = _to_decimal(row[4], allow_missing=False)
        else:
            return None
    except EngineInputError:
        return None
    if open_time is None or close_time is None:
        return None
    if open_ is None or high is None or low is None or close is None:
        return None
    if not all(v.is_finite() and v > 0 for v in (open_, high, low, close)):
        return None
    if high < low:
        return None
    return Candle(
        open_time=open_time,
        close_time=close_time,
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def closed_candles(
    rows: Optional[Sequence[Any]],
    *,
    now_ms: Optional[int] = None,
) -> list[Candle]:
    """Return only fully closed candles. A forming candle (close_time >= now) is dropped."""
    if not rows:
        return []
    if now_ms is None:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    out: list[Candle] = []
    for row in rows:
        candle = parse_candle(row)
        if candle is None:
            continue
        if candle.close_time >= now_ms:
            continue
        out.append(candle)
    out.sort(key=lambda c: c.open_time)
    return out


def sma_from_closes(closes: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """Simple moving average of the last ``period`` closes. None if short/invalid."""
    if period <= 0:
        return None
    values = list(closes)
    if len(values) < period:
        return None
    window = values[-period:]
    if any(v is None or not v.is_finite() for v in window):
        return None
    total = sum(window, Decimal("0"))
    return total / Decimal(period)


def compute_sma(candles: Sequence[Candle], period: int) -> IndicatorCompute:
    if period <= 0:
        return IndicatorCompute(None, None, "均线周期无效")
    if len(candles) < period:
        return IndicatorCompute(
            None,
            candles[-1] if candles else None,
            f"已收盘 K 线不足，无法计算均线（需要 {period} 根，当前 {len(candles)} 根）",
        )
    value = sma_from_closes([c.close for c in candles], period)
    if value is None or not value.is_finite():
        return IndicatorCompute(None, candles[-1], "均线值无效，未填写止损")
    return IndicatorCompute(value, candles[-1])


def _true_ranges(candles: Sequence[Candle]) -> list[Decimal]:
    trs: list[Decimal] = []
    prev_close: Optional[Decimal] = None
    for candle in candles:
        high_low = candle.high - candle.low
        if prev_close is None:
            tr = high_low
        else:
            tr = max(high_low, abs(candle.high - prev_close), abs(candle.low - prev_close))
        trs.append(tr)
        prev_close = candle.close
    return trs


def wilder_atr(candles: Sequence[Candle], period: int) -> list[Optional[Decimal]]:
    """Wilder ATR aligned to ``candles``. Values before the first full window are None."""
    if period <= 0 or not candles:
        return [None] * len(candles)
    trs = _true_ranges(candles)
    atrs: list[Optional[Decimal]] = [None] * len(candles)
    if len(trs) < period:
        return atrs
    first = sum(trs[:period], Decimal("0")) / Decimal(period)
    atrs[period - 1] = first
    prev = first
    for i in range(period, len(trs)):
        prev = (prev * Decimal(period - 1) + trs[i]) / Decimal(period)
        atrs[i] = prev
    return atrs


def compute_supertrend(
    candles: Sequence[Candle],
    period: int,
    multiplier: Union[int, float, Decimal, str],
) -> IndicatorCompute:
    """Standard Supertrend (HL2 / Wilder ATR). Returns the last closed value only."""
    if period <= 0:
        return IndicatorCompute(None, None, "Supertrend 周期无效")
    try:
        mult = multiplier if isinstance(multiplier, Decimal) else Decimal(str(multiplier))
    except Exception:
        return IndicatorCompute(None, None, "Supertrend 乘数无效")
    if not mult.is_finite() or mult <= 0:
        return IndicatorCompute(None, None, "Supertrend 乘数无效")
    if len(candles) < period:
        return IndicatorCompute(
            None,
            candles[-1] if candles else None,
            f"已收盘 K 线不足，无法计算 Supertrend（需要至少 {period} 根，当前 {len(candles)} 根）",
        )

    atrs = wilder_atr(candles, period)
    return _supertrend_loop(candles, atrs, mult)


def _supertrend_loop(
    candles: Sequence[Candle],
    atrs: Sequence[Optional[Decimal]],
    mult: Decimal,
) -> IndicatorCompute:
    prev_trend_up: Optional[Decimal] = None
    prev_trend_down: Optional[Decimal] = None
    prev_trend: int = 1
    last: Optional[Decimal] = None
    last_candle: Optional[Candle] = None

    for i, candle in enumerate(candles):
        atr = atrs[i]
        if atr is None or not atr.is_finite():
            continue
        hl2 = (candle.high + candle.low) / Decimal("2")
        up = hl2 - (mult * atr)
        down = hl2 + (mult * atr)
        prev_close = candles[i - 1].close if i > 0 else None

        if prev_trend_up is None or prev_trend_down is None or prev_close is None:
            trend_up = up
            trend_down = down
            trend = 1
        else:
            if prev_close > prev_trend_up:
                trend_up = up if up > prev_trend_up else prev_trend_up
            else:
                trend_up = up
            if prev_close < prev_trend_down:
                trend_down = down if down < prev_trend_down else prev_trend_down
            else:
                trend_down = down
            if candle.close > prev_trend_down:
                trend = 1
            elif candle.close < prev_trend_up:
                trend = -1
            else:
                trend = prev_trend

        last = trend_up if trend == 1 else trend_down
        last_candle = candle
        prev_trend_up = trend_up
        prev_trend_down = trend_down
        prev_trend = trend

    if last is None or not last.is_finite() or last_candle is None:
        return IndicatorCompute(
            None,
            candles[-1] if candles else None,
            "Supertrend 值无效，未填写止损",
        )
    return IndicatorCompute(last, last_candle)


def compute_indicator(
    candles: Sequence[Candle],
    source: str,
    *,
    ma_period: int,
    supertrend_atr_period: int,
    supertrend_multiplier: Union[int, float, Decimal, str],
) -> IndicatorCompute:
    if source == SOURCE_MA:
        return compute_sma(candles, ma_period)
    if source == SOURCE_SUPERTREND:
        return compute_supertrend(candles, supertrend_atr_period, supertrend_multiplier)
    return IndicatorCompute(None, None, "指标类型无效")


def _name(source: str) -> str:
    return "均线" if source == SOURCE_MA else "Supertrend"


def _period_name(source: str) -> str:
    return "当前周期均线" if source == SOURCE_MA else "当前周期 Supertrend "


def suggest_stop(
    *,
    interval: str,
    source: str,
    value: Optional[Decimal],
    mark: Optional[Decimal],
    side: Optional[str],
    tick_size: Optional[Decimal],
    candle: Optional[Candle] = None,
    params_label: str = "",
    error: Optional[str] = None,
) -> StopFill:
    """Decide whether an indicator value may auto-fill the stop. Never guesses."""

    def result(
        *,
        raw: Optional[Decimal] = None,
        fill: Optional[Decimal] = None,
        usable: bool = False,
        reason: str,
        mark_value: Optional[Decimal] = mark if isinstance(mark, Decimal) else None,
    ) -> StopFill:
        return StopFill(
            interval=interval,
            source=source,
            value=raw,
            fill=fill,
            usable_for_side=usable,
            reason=reason,
            candle=candle,
            mark=mark_value,
            params_label=params_label,
        )

    if error:
        return result(reason=error)

    if value is None or not value.is_finite():
        return result(reason="指标值无效，未填写止损")

    if mark is None or not mark.is_finite() or mark <= 0:
        return result(raw=value, reason="标记价格无效，未填写止损")

    name = _name(source)
    label = params_label or name
    interval_zh = interval_label(interval)
    where = candle_label(interval, candle)
    suffix = f"（{where}）" if where else ""

    def aligned(raw: Decimal, align_side: str) -> Decimal:
        if tick_size is not None and tick_size > 0:
            return align_stop(raw, tick_size, align_side)
        return raw

    def filled_reason(filled: Decimal) -> str:
        return f"{interval_zh} {label}：{_decimal_str(filled)}，已填入止损{suffix}"

    # No direction yet: only auto-fill a line strictly below the mark.
    if side is None:
        if value < mark:
            filled = aligned(value, "LONG")
            if filled >= mark:
                return result(
                    raw=value,
                    reason="对齐后的指标值未低于标记价格，未填写止损",
                )
            return result(raw=value, fill=filled, usable=True, reason=filled_reason(filled))
        if value > mark:
            return result(
                raw=value,
                reason=(
                    f"已算出{interval_zh}{_name(source)}在价格上方；未选择方向时只自动填入价格下方的线。"
                    "若要做空，请先选择做空后再试。"
                ),
            )
        return result(
            raw=value,
            reason=f"{_period_name(source)}未低于标记价格，未选择方向时不自动填写止损",
        )

    if side == "LONG":
        if value < mark:
            filled = aligned(value, "LONG")
            if filled >= mark:
                return result(
                    raw=value,
                    reason="对齐后的指标值未低于标记价格，不能作为多头止损",
                )
            return result(raw=value, fill=filled, usable=True, reason=filled_reason(filled))
        if value > mark:
            return result(
                raw=value,
                reason=f"{_period_name(source)}在价格上方，不能作为多头止损",
            )
        return result(
            raw=value,
            reason=f"{_period_name(source)}未低于标记价格，不能作为多头止损",
        )

    if side == "SHORT":
        if value > mark:
            filled = aligned(value, "SHORT")
            if filled <= mark:
                return result(
                    raw=value,
                    reason="对齐后的指标值未高于标记价格，不能作为空头止损",
                )
            return result(raw=value, fill=filled, usable=True, reason=filled_reason(filled))
        if value < mark:
            return result(
                raw=value,
                reason=f"{_period_name(source)}在价格下方，不能作为空头止损",
            )
        return result(
            raw=value,
            reason=f"{_period_name(source)}未高于标记价格，不能作为空头止损",
        )

    return result(raw=value, reason="方向无效，未填写止损")
