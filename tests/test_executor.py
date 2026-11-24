"""End-to-end executor tests: signal -> advisor -> guard -> simulator EA."""
import json
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from tools.fake_ea import FakeEa  # noqa: E402
from xau_monitor.advisor import (  # noqa: E402
    AdvisorContext, AdvisorDecision, AdvisorRejected, AdvisorUnavailable, NullAdvisor,
)
from xau_monitor.executor import ExecConfig, OrderExecutor  # noqa: E402
from xau_monitor.mt4_link import Mt4Link  # noqa: E402
from xau_monitor.orders import RiskConfig  # noqa: E402
from xau_monitor.strategy import Signal  # noqa: E402


class RejectAdvisor:
    name = "fake:reject"

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        return AdvisorDecision("reject", rationale="zona jebol ganda")


class BoomAdvisor:
    name = "fake:boom"

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        raise AdvisorUnavailable("timeout")


def make_signal(**over) -> Signal:
    base = dict(kind="ENTRY_SELL", price=2000.0, zone=2001.0, stop=2005.0,
                tps=[1990.0, 1980.0], invalid_level=2010.0, reasons=["D1:DOWN"])
    return Signal(**{**base, **over})


@pytest.fixture
def rig(tmp_path):
    link = Mt4Link(0, call_timeout=1.0)
    ea = FakeEa(link.port).start()
    deadline = time.time() + 3.0
    while not link.connected and time.time() < deadline:
        time.sleep(0.02)

    def build(advisor=None, risk=None, log_name="orders.jsonl"):
        cfg = ExecConfig(symbol="XAUUSD", magic=ea.magic, deviation_points=30,
                         dedupe_seconds=300, band_atr=0.5,
                         log_path=str(tmp_path / log_name))
        risk = risk or RiskConfig(risk_pct=1.0, max_lot=0.5, max_spread_points=50,
                                  max_basis=3.0, attach_tp=True)
        return OrderExecutor(link, advisor or NullAdvisor(), risk, cfg)

    yield link, ea, build, tmp_path
    ea.stop()
    link.shutdown()


def test_limit_order_at_engine_stop(rig):
    _link, ea, build, _tmp = rig
    text = build().execute(make_signal(), atr_m1=1.0)
    assert "LIMIT DIPASANG" in text
    assert len(ea.pending_orders) == 1 and ea.positions == []
    order = ea.pending_orders[0]
    assert order["side"] == "sell"
    assert order["price"] == pytest.approx(2005.0)   # level stop engine
    assert order["sl"] == 0.0 and order["tp"] == 0.0  # SL/TP dikosongkan
    assert order["lots"] == pytest.approx(0.2)


def test_limit_offset_moves_toward_market(rig):
    _link, ea, build, _tmp = rig
    # SELL: limit = SL - offset (mendekati pasar)
    build().execute(make_signal(limit_offset=0.5), atr_m1=1.0)
    assert ea.pending_orders[0]["price"] == pytest.approx(2004.5)

    # BUY: limit = SL + offset, tidak melewati harga pasar
    _link2, ea3, build2, _tmp2 = rig
    buy = make_signal(kind="ENTRY_BUY", price=2000.0, zone=1999.0, stop=1995.0,
                      tps=[2010.0], invalid_level=1990.0, limit_offset=0.5)
    build2(log_name="buy.jsonl").execute(buy, atr_m1=1.0)
    assert ea3.pending_orders[-1]["price"] == pytest.approx(1996.0)

    # offset besar di-clamp agar tetap di bawah ask (limit valid)
    huge = make_signal(kind="ENTRY_BUY", price=2000.0, zone=1999.0, stop=1995.0,
                       tps=[2010.0], invalid_level=1990.0, limit_offset=100.0)
    build2(log_name="buy2.jsonl").execute(huge, atr_m1=1.0)
    assert ea3.pending_orders[-1]["price"] < ea3.ask


def test_duplicate_signal_is_deduped(rig):
    _link, ea, build, _tmp = rig
    executor = build()
    sig = make_signal()
    assert "LIMIT DIPASANG" in executor.execute(sig, atr_m1=1.0)
    second = executor.execute(sig, atr_m1=1.0)
    assert "SINYAL SAMA" in second
    assert len(ea.sent_orders) == 1


def test_existing_position_blocks_new_order(rig):
    _link, ea, build, _tmp = rig
    ea.positions.append({"ticket": 777, "symbol": "XAUUSD", "type": "sell",
                         "lots": 0.1, "open": 2000.0, "sl": 2005.0, "tp": 1990.0,
                         "profit": 0.0, "comment": "x", "magic": ea.magic})
    text = build().execute(make_signal(), atr_m1=1.0)
    assert "POSISI SUDAH TERBUKA" in text and "#777" in text
    assert ea.sent_orders == []


def test_advisor_reject_blocks_order(rig):
    _link, ea, build, tmp_path = rig
    text = build(advisor=RejectAdvisor()).execute(make_signal(), atr_m1=1.0)
    assert "AI MENOLAK" in text
    assert ea.sent_orders == []
    rows = [json.loads(line) for line in
            (tmp_path / "orders.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "advisor_reject"


def test_advisor_unavailable_is_fail_closed(rig):
    _link, ea, build, _tmp = rig
    text = build(advisor=BoomAdvisor()).execute(make_signal(), atr_m1=1.0)
    assert "AI TIDAK TERSEDIA" in text
    assert ea.sent_orders == []


def test_guard_rejection_blocks_order(rig):
    _link, ea, build, tmp_path = rig
    ea.spread_points = 200
    text = build().execute(make_signal(), atr_m1=1.0)
    assert "DITOLAK GUARD" in text and "spread_too_wide" in text
    assert ea.sent_orders == []
    rows = [json.loads(line) for line in
            (tmp_path / "orders.jsonl").read_text().splitlines()]
    assert rows[-1]["code"] == "spread_too_wide"


def test_ea_rejection_is_reported(rig):
    _link, ea, build, _tmp = rig
    ea.allow_trading = False
    text = build().execute(make_signal(), atr_m1=1.0)
    assert "DITOLAK BROKER/EA" in text and "trading_disabled" in text
    assert ea.sent_orders == []


def test_bridge_down_is_reported(tmp_path):
    link = Mt4Link(0, call_timeout=0.3)
    cfg = ExecConfig(symbol="XAUUSD", magic=1, deviation_points=30,
                     dedupe_seconds=300, band_atr=0.5,
                     log_path=str(tmp_path / "orders.jsonl"))
    risk = RiskConfig(1.0, 0.5, 50, 3.0, True)
    try:
        text = OrderExecutor(link, NullAdvisor(), risk, cfg).execute(make_signal(), 1.0)
    finally:
        link.shutdown()
    assert "BRIDGE MT4 GAGAL" in text


def test_audit_line_written_on_success(rig):
    _link, _ea, build, tmp_path = rig
    build().execute(make_signal(), atr_m1=1.0)
    rows = [json.loads(line) for line in
            (tmp_path / "orders.jsonl").read_text().splitlines()]
    sent = [r for r in rows if r["event"] == "pending"]
    assert len(sent) == 1
    assert sent[0]["plan"]["lots"] == pytest.approx(0.2)
    assert sent[0]["symbol"] == "XAUUSD"
    assert sent[0]["layers"] == 1
    assert sent[0]["limits"][0] == pytest.approx(2005.0)


def test_advisor_adjust_within_band_is_used(rig):
    _link, ea, build, _tmp = rig

    class Tighten:
        name = "fake:adjust"

        def review(self, ctx):
            return AdvisorDecision("adjust", sl=2005.4, tps=(1990.2, 1980.2),
                                   confidence=0.6, rationale="geser sedikit")

    build(advisor=Tighten()).execute(make_signal(), atr_m1=1.0)
    # Level limit mengikuti SL hasil adjust advisor; SL/TP broker tetap kosong.
    assert ea.pending_orders[0]["price"] == pytest.approx(2005.4)
    assert ea.pending_orders[0]["sl"] == 0.0
    assert ea.pending_orders[0]["tp"] == 0.0


# ---- volume: usulan advisor, guard yang mengunci ---------------------

class SizingAdvisor:
    name = "fake:sizing"

    def __init__(self, lots: float) -> None:
        self._lots = lots

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        return AdvisorDecision("approve", lots=self._lots, rationale="ukuran dari pola")


def test_advisor_lots_determine_order_volume(rig):
    _link, ea, build, tmp_path = rig
    text = build(advisor=SizingAdvisor(0.03), log_name="sizing.jsonl").execute(
        make_signal(), atr_m1=1.0)
    assert "LIMIT DIPASANG" in text
    assert ea.pending_orders[0]["lots"] == pytest.approx(0.03)
    rows = [json.loads(line) for line in
            (tmp_path / "sizing.jsonl").read_text().splitlines()]
    assert rows[-1]["plan"]["lots"] == pytest.approx(0.03)
    assert rows[-1]["plan"]["lots_requested"] == pytest.approx(0.03)


def test_advisor_lots_clamped_by_guard_cap(rig):
    _link, ea, build, tmp_path = rig
    # RiskConfig default di rig: max_lot 0.5 -> usulan 3.0 harus dipotong.
    build(advisor=SizingAdvisor(3.0), log_name="sizing.jsonl").execute(
        make_signal(), atr_m1=1.0)
    assert ea.pending_orders[0]["lots"] == pytest.approx(0.5)
    rows = [json.loads(line) for line in
            (tmp_path / "sizing.jsonl").read_text().splitlines()]
    assert rows[-1]["plan"]["lots_requested"] == pytest.approx(3.0)


def test_advisor_lots_below_min_blocks_order(rig):
    _link, ea, build, tmp_path = rig
    text = build(advisor=SizingAdvisor(0.0001), log_name="sizing.jsonl").execute(
        make_signal(), atr_m1=1.0)
    assert "ORDER DITOLAK GUARD" in text and "lot_below_min" in text
    assert ea.sent_orders == []
    rows = [json.loads(line) for line in
            (tmp_path / "sizing.jsonl").read_text().splitlines()]
    assert rows[-1]["code"] == "lot_below_min"

