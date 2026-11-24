"""Tests for the deterministic order guard (pure logic, no broker)."""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.orders import (  # noqa: E402
    AccountState, GuardRejection, OrderIntent, RiskConfig, SymbolSpec,
    build_plan, floor_to_step,
)


def spec(**over) -> SymbolSpec:
    base = dict(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                stops_level_points=0, freeze_level_points=0, lot_min=0.01,
                lot_max=50.0, lot_step=0.01, margin_per_lot=20.0,
                spread_points=20, bid=2000.0, ask=2000.5, trade_allowed=True)
    return SymbolSpec(**{**base, **over})


def account(**over) -> AccountState:
    base = dict(login=1, server="Fake-Demo", currency="USD", equity=10_000.0,
                balance=10_000.0, free_margin=9_000.0, leverage=100)
    return AccountState(**{**base, **over})


def risk(**over) -> RiskConfig:
    base = dict(risk_pct=1.0, max_lot=0.5, max_spread_points=50, max_basis=3.0,
                attach_tp=True)
    return RiskConfig(**{**base, **over})


def sell_intent(**over) -> OrderIntent:
    base = dict(symbol="XAUUSD", side="SELL", ref_price=2000.0, stop=2005.0,
                tps=(1990.0, 1980.0), invalid=2010.0, zone=2001.0, mode="sniper",
                atr_m1=1.0)
    return OrderIntent(**{**base, **over})


def buy_intent(**over) -> OrderIntent:
    base = dict(symbol="XAUUSD", side="BUY", ref_price=2000.5, stop=1995.5,
                tps=(2010.0,), invalid=1990.0, zone=2000.0, mode="sniper",
                atr_m1=1.0)
    return OrderIntent(**{**base, **over})


def test_floor_to_step():
    assert floor_to_step(0.234, 0.01) == pytest.approx(0.23)
    assert floor_to_step(0.234, 0.1) == pytest.approx(0.2)
    with pytest.raises(ValueError):
        floor_to_step(1.0, 0)


def test_sell_plan_happy_path_sizes_and_attaches_nearest_tp():
    plan = build_plan(sell_intent(), spec(), account(), risk())
    assert plan.entry == pytest.approx(2000.0)      # SELL pakai bid
    assert plan.stop == pytest.approx(2005.0)
    assert plan.take_profit == pytest.approx(1990.0)  # .md: TP terdekat
    # risiko 1% dari 10.000 = 100 USD; SL 5 USD = 500 tick x 1.0
    assert plan.lots == pytest.approx(0.2)
    assert plan.risk_amount == pytest.approx(100.0)


def test_buy_plan_shifts_levels_by_basis():
    plan = build_plan(buy_intent(), spec(), account(), risk())
    assert plan.entry == pytest.approx(2000.5)   # BUY pakai ask
    assert plan.basis == pytest.approx(0.0)
    assert plan.stop == pytest.approx(1995.5)
    assert plan.take_profit == pytest.approx(2010.0)


def test_basis_shift_applied_to_all_levels():
    # feed 2000.0, broker bid 2000.4 -> semua level bergeser +0.4
    plan = build_plan(sell_intent(), spec(bid=2000.4), account(), risk())
    assert plan.basis == pytest.approx(0.4)
    assert plan.stop == pytest.approx(2005.4)
    assert plan.take_profit == pytest.approx(1990.4)


def test_basis_too_wide_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(bid=2010.0), account(), risk())
    assert exc.value.code == "basis_too_wide"


def test_spread_too_wide_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(spread_points=200), account(), risk())
    assert exc.value.code == "spread_too_wide"


def test_symbol_not_tradable_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(trade_allowed=False), account(), risk())
    assert exc.value.code == "symbol_disabled"


def test_stop_closer_than_broker_stoplevel_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(stop=2000.5), spec(stops_level_points=100), account(), risk())
    assert exc.value.code == "stop_too_close"


def test_tp_missing_when_attach_tp_enabled():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(tps=()), spec(), account(), risk())
    assert exc.value.code == "tp_missing"


def test_attach_tp_off_leaves_tp_none():
    plan = build_plan(sell_intent(tps=()), spec(), account(), risk(attach_tp=False))
    assert plan.take_profit is None


def test_lot_below_minimum_fails_loudly():
    # SL 0.50 USD -> risiko/lot 50 USD; 1% dari 10.000 = 100 -> 2 lot, tapi max 0.01 lot
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(stop=2000.5), spec(lot_min=1.0), account(), risk())
    assert exc.value.code == "lot_below_min"


def test_max_lot_clamp():
    plan = build_plan(sell_intent(stop=2000.5), spec(), account(), risk(max_lot=0.05))
    assert plan.lots == pytest.approx(0.05)


def test_advisor_lots_used_when_within_cap():
    plan = build_plan(sell_intent(), spec(), account(), risk(max_lot=0.5),
                      lots_requested=0.07)
    assert plan.lots == pytest.approx(0.07)
    assert plan.lots_requested == pytest.approx(0.07)


def test_advisor_lots_floored_to_step_and_clamped_to_cap():
    plan = build_plan(sell_intent(), spec(), account(), risk(max_lot=0.5),
                      lots_requested=0.078)
    assert plan.lots == pytest.approx(0.07)
    capped = build_plan(sell_intent(), spec(), account(), risk(max_lot=0.5),
                        lots_requested=9.0)
    assert capped.lots == pytest.approx(0.5)
    assert capped.lots_requested == pytest.approx(9.0)


def test_advisor_lots_below_minimum_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(), account(), risk(), lots_requested=0.001)
    assert exc.value.code == "lot_below_min"


def test_advisor_lots_nonpositive_is_rejected():
    for bad in (0.0, -0.02, float("nan"), float("inf")):
        with pytest.raises(GuardRejection) as exc:
            build_plan(sell_intent(), spec(), account(), risk(), lots_requested=bad)
        assert exc.value.code == "advisor_lot_invalid"


def test_margin_unknown_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(margin_per_lot=0.0), account(), risk())
    assert exc.value.code == "margin_unknown"


def test_margin_insufficient_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        build_plan(sell_intent(), spec(margin_per_lot=100_000.0), account(), risk())
    assert exc.value.code == "margin"


def test_geometry_violation_from_intent_is_rejected():
    with pytest.raises(GuardRejection) as exc:
        sell_intent(stop=1999.0)  # SELL stop harus di atas ref
    assert exc.value.code == "intent_invalid"


def test_geometry_violation_after_rounding_is_rejected():
    # jarak 0.004 membulat ke 2 digit -> stop == entry, harus ditolak (SELL)
    intent = sell_intent(stop=2000.004, tps=(1990.0,))
    with pytest.raises(GuardRejection) as exc:
        build_plan(intent, spec(), account(), risk())
    assert exc.value.code == "geometry"


def test_spec_validation_rejects_broken_fields():
    base = {"sym": "XAUUSD", "digits": 2, "point": 0.01, "ticksize": 0.01,
            "tickvalue": 1.0, "stoplevel": 0, "freezelevel": 0, "lotmin": 0.01,
            "lotmax": 50.0, "lotstep": 0.01, "marginreq": 20.0, "spread": 20,
            "bid": 2000.0, "ask": 2000.5, "tradeallowed": "1"}
    wire = {k: str(v) for k, v in base.items()}
    assert SymbolSpec.from_wire(wire).digits == 2
    for broken in ({"tickvalue": "0"}, {"bid": "0"}, {"bid": "2001"}):
        with pytest.raises(GuardRejection):
            SymbolSpec.from_wire({**wire, **broken})


def test_account_validation():
    with pytest.raises(GuardRejection):
        account(equity=0.0)


def test_risk_config_validation():
    with pytest.raises(GuardRejection):
        risk(risk_pct=0.0).validate()
    with pytest.raises(GuardRejection):
        risk(risk_pct=10.0).validate()
