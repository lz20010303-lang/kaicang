"""Pure risk engine: no I/O, no exchange calls, deterministic PASS/REJECT."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any, Optional, Union

RISK_FRACTION = Decimal("0.05")
LEVERAGE = 20
MARGIN_MODE = "CROSSED"

Number = Union[str, int, float, Decimal, None]


@dataclass(frozen=True)
class SymbolFilters:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_raw(
        cls,
        tick_size: Number,
        step_size: Number,
        min_qty: Number,
        min_notional: Number,
    ) -> Optional["SymbolFilters"]:
        try:
            tick = _to_decimal(tick_size, allow_missing=False)
            step = _to_decimal(step_size, allow_missing=False)
            qty = _to_decimal(min_qty, allow_missing=False)
            notion = _to_decimal(min_notional, allow_missing=False)
        except EngineInputError:
            return None
        if tick is None or step is None or qty is None or notion is None:
            return None
        if tick <= 0 or step <= 0 or qty <= 0 or notion <= 0:
            return None
        return cls(tick_size=tick, step_size=step, min_qty=qty, min_notional=notion)


@dataclass(frozen=True)
class Decision:
    status: str
    reason_code: str
    reason_zh: str
    side: Optional[str] = None
    mark: Optional[Decimal] = None
    stop: Optional[Decimal] = None
    risk_usd: Optional[Decimal] = None
    risk_fraction: Decimal = RISK_FRACTION
    quantity: Optional[Decimal] = None
    raw_quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    leverage: int = LEVERAGE
    margin_mode: str = MARGIN_MODE

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        def fmt(value: Optional[Decimal]) -> Optional[str]:
            if value is None:
                return None
            return _decimal_str(value)

        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
            "side": self.side,
            "mark": fmt(self.mark),
            "stop": fmt(self.stop),
            "risk_usd": fmt(self.risk_usd),
            "risk_fraction": str(self.risk_fraction),
            "quantity": fmt(self.quantity),
            "raw_quantity": fmt(self.raw_quantity),
            "notional": fmt(self.notional),
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
        }


class EngineInputError(ValueError):
    """Non-finite or unparseable numeric input."""


def _to_decimal(value: Number, *, allow_missing: bool = True) -> Optional[Decimal]:
    if value is None:
        if allow_missing:
            return None
        raise EngineInputError("missing")
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            if allow_missing:
                return None
            raise EngineInputError("empty")
        value = text
    if isinstance(value, bool):
        raise EngineInputError("bool")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise EngineInputError("non-finite")
        value = str(value)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EngineInputError("unparseable") from exc
    if not number.is_finite():
        raise EngineInputError("non-finite")
    return number


def _decimal_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _quantize_down(value: Decimal, step: Decimal) -> Decimal:
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def align_stop(stop: Decimal, tick: Decimal, side: str) -> Decimal:
    """Snap stop to tick without tightening risk: floor for long, ceil for short."""
    ratio = stop / tick
    if side == "LONG":
        steps = ratio.to_integral_value(rounding=ROUND_DOWN)
    else:
        steps = ratio.to_integral_value(rounding=ROUND_UP)
    return steps * tick


# Backward-compatible alias used by older call sites / tests.
_align_stop = align_stop


def _reject(
    code: str,
    reason_zh: str,
    *,
    side: Optional[str] = None,
    mark: Optional[Decimal] = None,
    stop: Optional[Decimal] = None,
    risk_usd: Optional[Decimal] = None,
    quantity: Optional[Decimal] = None,
    raw_quantity: Optional[Decimal] = None,
    notional: Optional[Decimal] = None,
) -> Decision:
    return Decision(
        status="REJECT",
        reason_code=code,
        reason_zh=reason_zh,
        side=side,
        mark=mark,
        stop=stop,
        risk_usd=risk_usd,
        quantity=quantity,
        raw_quantity=raw_quantity,
        notional=notional,
    )


def _normalize_side(side: Optional[str]) -> Optional[str]:
    if side is None:
        return None
    text = str(side).strip().upper()
    if text in {"LONG", "BUY"}:
        return "LONG"
    if text in {"SHORT", "SELL"}:
        return "SHORT"
    if text == "":
        return None
    return "__INVALID__"


def evaluate(
    *,
    side: Optional[str],
    stop: Number,
    mark: Number,
    balance: Number,
    filters: Optional[SymbolFilters],
    quantity: Number = None,
) -> Decision:
    """Evaluate an opening ticket.

    ``quantity`` is accepted only so callers cannot sneak in a size: it is
    always ignored. Position size is risk_usd / |mark - stop|, rounded down.
    """
    _ = quantity  # intentionally ignored

    try:
        mark_d = _to_decimal(mark, allow_missing=False)
        balance_d = _to_decimal(balance, allow_missing=False)
    except EngineInputError:
        return _reject("INVALID_INPUT", "输入数据缺失或无效（余额或标记价格）")

    if mark_d is None or balance_d is None or mark_d <= 0 or balance_d < 0:
        return _reject("INVALID_INPUT", "输入数据缺失或无效（余额或标记价格）")

    if filters is None:
        return _reject("INVALID_INPUT", "输入数据缺失或无效（合约过滤器）", mark=mark_d)

    side_n = _normalize_side(side)
    if side_n is None:
        return _reject("INVALID_SIDE", "请选择开仓方向", mark=mark_d)
    if side_n == "__INVALID__":
        return _reject("INVALID_SIDE", "方向无效，只能是做多或做空", mark=mark_d)

    if stop is None or (isinstance(stop, str) and stop.strip() == ""):
        return _reject("MISSING_STOP", "止损价为必填项，不填不允许开单", side=side_n, mark=mark_d)

    try:
        stop_d = _to_decimal(stop, allow_missing=False)
    except EngineInputError:
        return _reject("INVALID_STOP", "止损价无效", side=side_n, mark=mark_d)

    if stop_d is None or stop_d <= 0:
        return _reject("INVALID_STOP", "止损价无效", side=side_n, mark=mark_d)

    aligned_stop = align_stop(stop_d, filters.tick_size, side_n)

    if side_n == "LONG" and aligned_stop >= mark_d:
        return _reject(
            "STOP_NOT_BELOW_MARK",
            "做多时止损价必须低于标记价格",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
        )
    if side_n == "SHORT" and aligned_stop <= mark_d:
        return _reject(
            "STOP_NOT_ABOVE_MARK",
            "做空时止损价必须高于标记价格",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
        )

    distance = abs(mark_d - aligned_stop)
    if distance <= 0 or not distance.is_finite():
        return _reject(
            "ZERO_DISTANCE",
            "止损距离必须大于 0",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
        )

    risk_usd = balance_d * RISK_FRACTION
    if risk_usd <= 0 or not risk_usd.is_finite():
        return _reject(
            "INVALID_INPUT",
            "风险金额无效（账户余额需大于 0）",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
            risk_usd=risk_usd,
        )

    raw_qty = risk_usd / distance
    qty = _quantize_down(raw_qty, filters.step_size)

    if qty < filters.min_qty or qty <= 0:
        return _reject(
            "QTY_BELOW_MIN",
            "计算数量不足最小下单量，已拒绝（不会向上取整）",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
            risk_usd=risk_usd,
            quantity=qty,
            raw_quantity=raw_qty,
        )

    notional = qty * mark_d
    if notional < filters.min_notional:
        return _reject(
            "NOTIONAL_BELOW_MIN",
            "名义价值不足最小名义金额，已拒绝（不会向上取整凑手数）",
            side=side_n,
            mark=mark_d,
            stop=aligned_stop,
            risk_usd=risk_usd,
            quantity=qty,
            raw_quantity=raw_qty,
            notional=notional,
        )

    return Decision(
        status="PASS",
        reason_code="PASS",
        reason_zh="通过",
        side=side_n,
        mark=mark_d,
        stop=aligned_stop,
        risk_usd=risk_usd,
        quantity=qty,
        raw_quantity=raw_qty,
        notional=notional,
    )


NOT_IN_WATCHLIST_ZH = "该交易对不在自选中，请先加入自选后再开单"


def require_in_watchlist(symbol: Optional[str], watchlist: Optional[list[str]]) -> Optional[Decision]:
    """Hard gate: a symbol may be opened only if it is already in the watchlist.

    ``watchlist`` must be provided by the caller (loaded from storage). This
    function is pure and performs no I/O. Existing ``evaluate`` tests are
    unchanged because they do not call this gate.
    """
    if watchlist is None:
        return _reject("NOT_IN_WATCHLIST", NOT_IN_WATCHLIST_ZH)
    allowed = {str(item).strip().upper() for item in watchlist if str(item).strip()}
    sym = (symbol or "").strip().upper()
    if not sym or sym not in allowed:
        return _reject("NOT_IN_WATCHLIST", NOT_IN_WATCHLIST_ZH)
    return None
