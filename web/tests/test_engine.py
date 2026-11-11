"""Unit tests for the pure risk engine. No I/O, no network."""

from decimal import Decimal

from app.engine import SymbolFilters, evaluate

BTC = SymbolFilters(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("100"),
)

# Convenient round numbers used by several cases.
MARK = Decimal("100000")
BALANCE = Decimal("1000")  # risk_usd = 50


def ev(**kwargs):
    defaults = dict(mark=MARK, balance=BALANCE, filters=BTC)
    defaults.update(kwargs)
    return evaluate(**defaults)


def test_missing_stop_none():
    d = ev(side="LONG", stop=None)
    assert d.status == "REJECT"
    assert d.reason_code == "MISSING_STOP"
    assert "必填" in d.reason_zh


def test_missing_stop_empty_string():
    d = ev(side="SHORT", stop="  ")
    assert d.status == "REJECT"
    assert d.reason_code == "MISSING_STOP"


def test_invalid_stop_non_numeric():
    d = ev(side="LONG", stop="abc")
    assert d.status == "REJECT"
    assert d.reason_code == "INVALID_STOP"


def test_invalid_stop_non_finite():
    d = ev(side="LONG", stop=float("inf"))
    assert d.status == "REJECT"
    assert d.reason_code == "INVALID_STOP"
    d2 = ev(side="LONG", stop=float("nan"))
    assert d2.status == "REJECT"
    assert d2.reason_code == "INVALID_STOP"


def test_stop_on_wrong_side_long():
    d = ev(side="LONG", stop=MARK)
    assert d.status == "REJECT"
    assert d.reason_code == "STOP_NOT_BELOW_MARK"

    d2 = ev(side="LONG", stop=Decimal("100001"))
    assert d2.status == "REJECT"
    assert d2.reason_code == "STOP_NOT_BELOW_MARK"


def test_stop_on_wrong_side_short():
    d = ev(side="SHORT", stop=MARK)
    assert d.status == "REJECT"
    assert d.reason_code == "STOP_NOT_ABOVE_MARK"

    d2 = ev(side="SHORT", stop=Decimal("99999"))
    assert d2.status == "REJECT"
    assert d2.reason_code == "STOP_NOT_ABOVE_MARK"


def test_size_is_five_percent_over_distance_rounded_down():
    # risk = 50, distance = 3200, raw qty = 50/3200 = 0.015625
    # step 0.001 -> round DOWN to 0.015 (not 0.016)
    stop = MARK - Decimal("3200")
    d = ev(side="LONG", stop=stop)
    assert d.status == "PASS"
    assert d.risk_usd == Decimal("50")
    assert d.raw_quantity == Decimal("50") / Decimal("3200")
    assert d.quantity == Decimal("0.015")
    assert d.notional == Decimal("0.015") * MARK


def test_size_formula_short():
    # same distance on the short side
    stop = MARK + Decimal("3200")
    d = ev(side="SHORT", stop=stop)
    assert d.status == "PASS"
    assert d.risk_usd == Decimal("50")
    assert d.quantity == Decimal("0.015")


def test_no_rounding_up():
    # Tick-aligned stop so alignment does not change the distance.
    # distance = 1724, raw qty = 50/1724 ≈ 0.0289965, step 0.01 -> 0.02, never 0.03
    filters = SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("100"),
    )
    stop = MARK - Decimal("1724")  # 98276, already on tick
    d = ev(side="LONG", stop=stop, filters=filters)
    raw = Decimal("50") / Decimal("1724")
    assert d.status == "PASS"
    assert d.raw_quantity == raw
    assert d.quantity == Decimal("0.02")
    assert d.quantity < d.raw_quantity
    assert d.quantity + filters.step_size > d.raw_quantity


def test_below_min_notional_reject():
    # qty will be 0.05 after sizing, notional = 5000; raise min notional above that
    filters = SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10000"),
    )
    d = ev(side="LONG", stop=Decimal("99000"), filters=filters)
    assert d.quantity == Decimal("0.05")
    assert d.notional == Decimal("5000")
    assert d.status == "REJECT"
    assert d.reason_code == "NOTIONAL_BELOW_MIN"


def test_below_min_qty_reject():
    # huge distance -> raw qty < minQty; after round down qty = 0
    d = ev(side="LONG", stop=Decimal("40000"))
    # distance = 60000, raw = 50/60000 = 0.000833... -> 0
    assert d.status == "REJECT"
    assert d.reason_code == "QTY_BELOW_MIN"
    assert d.quantity == Decimal("0")


def test_valid_long_pass():
    d = ev(side="LONG", stop=Decimal("99000"))
    assert d.status == "PASS"
    assert d.reason_code == "PASS"
    assert d.side == "LONG"
    assert d.stop == Decimal("99000")
    assert d.risk_usd == Decimal("50")
    # 50 / 1000 = 0.05, already on step
    assert d.quantity == Decimal("0.05")
    assert d.notional == Decimal("5000")
    assert d.leverage == 20
    assert d.margin_mode == "CROSSED"


def test_valid_short_pass():
    d = ev(side="SHORT", stop=Decimal("101000"))
    assert d.status == "PASS"
    assert d.side == "SHORT"
    assert d.stop == Decimal("101000")
    assert d.quantity == Decimal("0.05")
    assert d.notional == Decimal("5000")


def test_engine_ignores_user_supplied_size():
    normal = ev(side="LONG", stop=Decimal("99000"))
    sneaky = ev(side="LONG", stop=Decimal("99000"), quantity=Decimal("999"))
    also = ev(side="LONG", stop=Decimal("99000"), quantity="0.001")
    assert normal.status == "PASS"
    assert sneaky.status == "PASS"
    assert also.status == "PASS"
    assert sneaky.quantity == normal.quantity == Decimal("0.05")
    assert also.quantity == normal.quantity
    assert sneaky.quantity != Decimal("999")


def test_missing_or_non_finite_mark_and_balance():
    d = ev(side="LONG", stop=Decimal("99000"), mark=None)
    assert d.status == "REJECT"
    assert d.reason_code == "INVALID_INPUT"

    d2 = ev(side="LONG", stop=Decimal("99000"), mark=float("nan"))
    assert d2.status == "REJECT"
    assert d2.reason_code == "INVALID_INPUT"

    d3 = ev(side="LONG", stop=Decimal("99000"), balance=float("-inf"))
    assert d3.status == "REJECT"
    assert d3.reason_code == "INVALID_INPUT"


def test_invalid_side():
    d = ev(side=None, stop=Decimal("99000"))
    assert d.status == "REJECT"
    assert d.reason_code == "INVALID_SIDE"
    d2 = ev(side="FLAT", stop=Decimal("99000"))
    assert d2.status == "REJECT"
    assert d2.reason_code == "INVALID_SIDE"


def test_zero_balance_rejected():
    d = ev(side="LONG", stop=Decimal("99000"), balance=Decimal("0"))
    assert d.status == "REJECT"
    assert d.reason_code == "INVALID_INPUT"


def test_side_aliases():
    d = ev(side="buy", stop=Decimal("99000"))
    assert d.status == "PASS"
    assert d.side == "LONG"
    d2 = ev(side="SELL", stop=Decimal("101000"))
    assert d2.status == "PASS"
    assert d2.side == "SHORT"


def test_stop_aligned_to_tick_without_tightening():
    # Long stop 99000.04 floors to 99000.0 (farther), distance 1000, qty 0.05
    d = ev(side="LONG", stop=Decimal("99000.04"))
    assert d.status == "PASS"
    assert d.stop == Decimal("99000.0") or d.stop == Decimal("99000")
    assert d.quantity == Decimal("0.05")

    # Short stop 101000.04 ceils to 101000.1 (farther)
    d2 = ev(side="SHORT", stop=Decimal("101000.04"))
    assert d2.status == "PASS"
    assert d2.stop == Decimal("101000.1")
