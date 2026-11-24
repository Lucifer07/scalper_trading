"""Presisi per-simbol: level FX tidak boleh dibulatkan seperti emas.

Regresi yang dicegah: ``round(x, 2)`` pada GBPUSD membuat semua level menjadi
1.32 dan buffer stop minimum 0,5 absolut menjadi ~1700 pip.
"""
from types import SimpleNamespace

from xau_monitor.advisor import AdvisorContext, build_system_prompt
from xau_monitor.candles import Candle, TFSnap
from xau_monitor.executor import ExecConfig, OrderExecutor, snapshot_metrics
from xau_monitor.orders import (
    AccountState, OrderIntent, RiskConfig, SymbolSpec, build_plan,
)
from xau_monitor.runner import _fmt_free
from xau_monitor.status import payload as status_payload
from xau_monitor.strategy import MIN_BUFFER_POINTS, Engine, Signal


def _snap(tf: str, atr: float) -> TFSnap:
    bars = [Candle(0, 1.32, 1.3201, 1.3199, 1.32) for _ in range(20)]
    return TFSnap(tf, bars, 1.32, atr, 50.0, [], [], (0.1, 0.1), 0)


def test_min_buffer_scales_with_broker_digits():
    assert Engine(digits=2).min_buffer() == 0.5              # XAUUSD: 0,50 USD
    assert Engine(digits=5).min_buffer() == 0.0005           # GBPUSD: 5 pip
    assert Engine(digits=5).min_buffer() == MIN_BUFFER_POINTS * 10 ** -5


def test_engine_rounds_levels_to_broker_digits():
    assert Engine(digits=5)._r(1.321054) == 1.32105
    assert Engine(digits=2)._r(3861.5678) == 3861.57


def test_advisor_facts_keep_fx_precision():
    ctx = AdvisorContext(
        symbol="GBPUSD", side="BUY", mode="handgun", price=1.321054,
        zone=1.3209, stop=1.32045, tps=(1.322,), invalid=1.3200,
        atr_m1=0.000234, reasons=(), digits=5)
    facts = ctx.facts()
    assert facts["engine_stop"] == 1.32045
    assert facts["price_feed"] == 1.32105
    assert facts["atr_m1"] == 0.000234


def test_system_prompt_names_actual_symbol():
    prompt = build_system_prompt("aturan", 0.5, True, "GBPUSD")
    assert "trading GBPUSD" in prompt
    assert "trading XAUUSD" not in prompt


def test_advisor_calibration_not_self_contradictory():
    from xau_monitor.advisor import CALIBRATION, build_system_prompt

    # Sebelum perbaikan, kalibrasi memuat dua aturan yang saling membatalkan:
    # "ekstrem: pos20 <=20% sell / >=80% buy" DAN "pos20 ~100% = chasing BUY".
    assert "sniper: D1+H4+H1 hanya menentukan ARAH" in CALIBRATION
    assert "ekstrem handgun" in CALIBRATION
    assert "M15 pos20 <=20%" in CALIBRATION
    assert "ekstrem: pos20 <=20% (sell)" not in CALIBRATION
    assert "Chasing diukur dari pos20 TF ENTRY" in CALIBRATION
    prompt = build_system_prompt("aturan", 0.5, False, "GBPUSD")
    assert "Untuk GBPUSD, abaikan semua angka contoh" in prompt


def test_generic_rules_file_for_non_xau_symbols():
    from pathlib import Path

    p = Path("/home/jayz/xau-monitor/docs/rules-price-action-generic.md")
    assert p.is_file()
    body = p.read_text(encoding="utf-8")
    assert "XAU" not in body and "Rp" not in body
    assert len(body) < 1500  # RULES_MD_MAX_CHARS default


def test_snapshot_metrics_atr_not_zero_for_fx():
    snaps = {"M1": _snap("M1", 0.000234), "M5": _snap("M5", 0.000351),
             "M15": _snap("M15", 0.0009)}
    metrics = dict(snapshot_metrics(snaps, 1.321, 1.3209, digits=5))
    assert metrics["atr_m1"] == 0.000234
    assert metrics["m5_thrust_threshold"] > 0


# ---- tampilan mengikuti digits broker (regresi 2 desimal hardcode) ----

def _fx_spec(**over) -> SymbolSpec:
    base = dict(name="GBPUSD", digits=5, point=0.00001, tick_size=0.00001,
                tick_value=0.1, stops_level_points=0, freeze_level_points=0,
                lot_min=0.01, lot_max=10.0, lot_step=0.01, margin_per_lot=500.0,
                spread_points=12, bid=1.32012, ask=1.32024, trade_allowed=True)
    return SymbolSpec(**{**base, **over})


def _executor() -> OrderExecutor:
    cfg = ExecConfig(symbol="GBPUSD", magic=1, deviation_points=10,
                     dedupe_seconds=60, band_atr=1.0, log_path="/dev/null")
    return OrderExecutor(None, SimpleNamespace(name="stub"),
                         RiskConfig(risk_pct=1.0, max_lot=1.0,
                                    max_spread_points=50, max_basis=0.001,
                                    attach_tp=True),
                         cfg)


def test_status_payload_rounds_price_to_broker_digits():
    st = status_payload(symbol="GBPUSD", price=1.320054, uptime_s=1.0,
                        tick={}, engines={}, orders={}, broker={},
                        dry_run=True, digits=5)
    assert st["price"] == 1.32005 and st["digits"] == 5
    st = status_payload(symbol="XAUUSD", price=3872.276, uptime_s=1.0,
                        tick={}, engines={}, orders={}, broker={},
                        dry_run=True, digits=2)
    assert st["price"] == 3872.28 and st["digits"] == 2


def test_fmt_free_keeps_price_decimals():
    assert _fmt_free(1.08543) == "1.08543"
    assert _fmt_free(3872.27) == "3872.27"
    assert _fmt_free(1.1) == "1.1"


def test_dedupe_key_separates_fx_levels():
    ex = _executor()
    base = dict(price=1.3201, zone=1.3205, stop=1.3245, tps=[1.3120],
                invalid_level=1.3260, ts=1000.0)
    near = Signal("ENTRY_SELL", **base)
    far = Signal("ENTRY_SELL", **{**base, "zone": 1.32053})
    assert ex._dedupe_key(near, "SELL") != ex._dedupe_key(far, "SELL")
    same = Signal("ENTRY_SELL", **base)
    assert ex._dedupe_key(near, "SELL") == ex._dedupe_key(same, "SELL")


def test_limit_msg_uses_broker_digits_and_empty_sltp():
    ex = _executor()
    sig = Signal("ENTRY_SELL", price=1.32012, zone=1.32050, stop=1.32450,
                 tps=[1.31200], invalid_level=1.32600, mode="handgun")
    plan = SimpleNamespace(stop=None, take_profit=None, lots=0.01,
                           entry=1.32012, basis=0.00012, risk_amount=1.0)
    msg = ex._limit_msg(sig, plan, {"ticket": 123},
                        SimpleNamespace(confidence=0.8, rationale="ok"),
                        "SELL", 5, 1.32450, 123)
    assert "LIMIT DIPASANG" in msg
    assert "lot @ 1.32450" in msg
    assert "SL/TP broker: kosong" in msg
    assert "Basis +0.00012" in msg


def test_fx_plan_reasons_keep_five_digits():
    intent = OrderIntent(symbol="GBPUSD", side="SELL", ref_price=1.32000,
                         stop=1.32450, tps=(1.31200,), invalid=1.32600,
                         zone=1.32050, mode="handgun", atr_m1=0.0002)
    plan = build_plan(
        intent, _fx_spec(),
        AccountState(login=1, server="Fake", currency="USD", equity=10_000.0,
                     balance=10_000.0, free_margin=9_000.0, leverage=100),
        RiskConfig(risk_pct=1.0, max_lot=1.0, max_spread_points=50,
                   max_basis=0.001, attach_tp=True))
    assert plan.reasons[0] == "basis +0.00012"
    assert "SL 0.00450" in plan.reasons[1] and "poin" in plan.reasons[1]
