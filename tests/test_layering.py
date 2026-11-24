"""Layering: beberapa limit berlapis; jumlah dari AI, dibatasi margin."""
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from tools.fake_ea import FakeEa  # noqa: E402
from xau_monitor.advisor import AdvisorDecision  # noqa: E402
from xau_monitor.executor import ExecConfig, OrderExecutor  # noqa: E402
from xau_monitor.mt4_link import Mt4Link  # noqa: E402
from xau_monitor.orders import RiskConfig  # noqa: E402
from xau_monitor.strategy import Signal  # noqa: E402


class LayerAdvisor:
    name = "fake:layers"

    def __init__(self, n: int) -> None:
        self.n = n

    def review(self, ctx):
        facts = ctx.facts()
        assert facts["layering"] is True
        assert facts["max_layers_by_margin"] >= 0
        return AdvisorDecision("approve", rationale="layering", layers=self.n)


def _signal(**over) -> Signal:
    base = dict(kind="ENTRY_SELL", price=2000.0, zone=2001.0, stop=2005.0,
                tps=[1990.0, 1980.0], invalid_level=2010.0, reasons=["x"],
                limit_offset=0.5, mode="sniper")
    return Signal(**{**base, **over})


def _exec(link, ea, advisor, tmp_path, **cfg_over) -> OrderExecutor:
    cfg = ExecConfig(symbol="XAUUSD", magic=ea.magic, deviation_points=30,
                     dedupe_seconds=300, band_atr=0.5,
                     log_path=str(tmp_path / "orders.jsonl"), **cfg_over)
    risk = RiskConfig(risk_pct=1.0, max_lot=0.5, max_spread_points=50,
                      max_basis=3.0, attach_tp=True)
    return OrderExecutor(link, advisor, risk, cfg)


@pytest.fixture
def rig(tmp_path):
    link = Mt4Link(0, call_timeout=1.0)
    ea = FakeEa(link.port).start()
    deadline = time.time() + 3.0
    while not link.connected and time.time() < deadline:
        time.sleep(0.02)
    yield link, ea, tmp_path
    ea.stop()
    link.shutdown()


def test_layering_places_requested_limits(rig):
    link, ea, tmp = rig
    ex = _exec(link, ea, LayerAdvisor(3), tmp,
               layering_enabled=True, layering_min=2)
    text = ex.execute(_signal(), 1.0)
    assert "layering" in text
    prices = [o["price"] for o in ea.pending_orders]
    assert prices == pytest.approx([2004.5, 2005.0, 2005.5])
    assert all(o["sl"] == 0 and o["tp"] == 0 for o in ea.pending_orders)


def test_layering_minimum_two_when_ai_returns_one(rig):
    link, ea, tmp = rig
    ex = _exec(link, ea, LayerAdvisor(1), tmp,
               layering_enabled=True, layering_min=2)
    ex.execute(_signal(), 1.0)
    assert len(ea.pending_orders) == 2


def test_layering_off_places_single_limit(rig):
    from xau_monitor.advisor import NullAdvisor

    link, ea, tmp = rig
    ex = _exec(link, ea, NullAdvisor(), tmp, layering_enabled=False)
    ex.execute(_signal(), 1.0)
    assert len(ea.pending_orders) == 1


def test_layering_rejected_when_margin_insufficient(tmp_path):
    link = Mt4Link(0, call_timeout=1.0)
    ea = FakeEa(link.port, equity=1000.0, free_margin=0.5).start()
    deadline = time.time() + 3.0
    while not link.connected and time.time() < deadline:
        time.sleep(0.02)
    try:
        ex = _exec(link, ea, LayerAdvisor(3), tmp_path,
                   layering_enabled=True, layering_min=2)
        text = ex.execute(_signal(), 1.0)
        assert "LAYERING DITOLAK" in text
        assert ea.pending_orders == []
    finally:
        ea.stop()
        link.shutdown()


def test_layering_spacing_floor_one_pip(rig):
    # Offset sangat kecil (0.001 < 1 pip XAU=0.01) -> jarak layer wajib 1 pip.
    link, ea, tmp = rig
    ex = _exec(link, ea, LayerAdvisor(3), tmp,
               layering_enabled=True, layering_min=2)
    ex.execute(_signal(limit_offset=0.001), 1.0)
    prices = [o["price"] for o in ea.pending_orders]
    assert prices == pytest.approx([2005.0, 2005.01, 2005.02])


def test_layering_bounded_by_total_exposure_not_layer_count(rig):
    # Tidak ada batas jumlah layer: 100 layer lolos bila eksposur (2% equity) cukup.
    link, ea, tmp = rig
    ex = _exec(link, ea, LayerAdvisor(100), tmp,
               layering_enabled=True, layering_min=2)
    ex.execute(_signal(stop=2010.0, limit_offset=0.5), 1.0)
    assert len(ea.pending_orders) == 100


def test_layering_exposure_cap_reduces_layers(rig):
    # Eksposur 0,5% equity membatasi jumlah layer walau AI minta 100.
    link, ea, tmp = rig
    ex = _exec(link, ea, LayerAdvisor(100), tmp,
               layering_enabled=True, layering_min=2,
               layering_max_margin_pct=0.5)
    ex.execute(_signal(stop=2010.0, limit_offset=0.5), 1.0)
    prices = [o["price"] for o in ea.pending_orders]
    assert 2 <= len(prices) < 100


def test_advisor_parses_layers():
    from xau_monitor.advisor import parse_decision

    d = parse_decision('{"action":"approve","layers":4,"rationale":"ok"}')
    assert d.layers == 4
    d = parse_decision('{"action":"approve","layers":"x"}')
    assert d.layers == 0
    d = parse_decision('{"action":"approve","layers":-2}')
    assert d.layers == 0
