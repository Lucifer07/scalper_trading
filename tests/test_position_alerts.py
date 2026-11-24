"""Gerbang alert manajemen posisi (TP*/INVALIDATED) — kasus phantom alert.

Kejadian pemicu (19-11-2026, EURUSD 20:23): sinyal ENTRY_SELL terkirim ke
Telegram, jalur order gagal di advisor (timeout 60s), engine tetap menyimpan
rencana TP sebagai 'paper position' lalu mengirim 'TP2 tercapai — close
manual' padahal tidak pernah ada posisi di broker.
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.order_flow import OrderFlow, position_alert_deliverable  # noqa: E402


def _sig(kind: str, mode: str = "handgun"):
    return SimpleNamespace(kind=kind, mode=mode)


def _flow(*, armed: bool = True, modes_with_position: tuple = ()):
    """OrderFlow ringan: hanya dict executors yang dipakai helper ini."""
    flow = OrderFlow(None, None)
    flow.executors = {} if not armed else {
        mode: SimpleNamespace(
            has_position=lambda m=mode: m in modes_with_position)
        for mode in ("sniper", "handgun")}
    return flow


# ---- mode manual (orders None / belum armed): alert level tetap keluar ----

def test_manual_mode_orders_none_delivers_tp():
    assert position_alert_deliverable(_sig("TP2"), None) is True


def test_unarmed_flow_delivers_tp():
    assert position_alert_deliverable(_sig("TP2"), _flow(armed=False)) is True


# ---- armed tanpa posisi: TP/INVALIDATED dibungkam --------------------------

def test_armed_without_position_suppresses_tp_and_invalidated():
    flow = _flow(modes_with_position=())
    assert position_alert_deliverable(_sig("TP1"), flow) is False
    assert position_alert_deliverable(_sig("TP2"), flow) is False
    assert position_alert_deliverable(_sig("INVALIDATED"), flow) is False


def test_armed_with_position_delivers_tp_and_invalidated():
    flow = _flow(modes_with_position=("handgun",))
    assert position_alert_deliverable(_sig("TP2"), flow) is True
    assert position_alert_deliverable(_sig("INVALIDATED"), flow) is True


def test_tp_scoped_per_mode_not_sibling():
    # Posisi handgun tidak boleh melegalkan TP rencana sniper (mode lain).
    flow = _flow(modes_with_position=("handgun",))
    assert position_alert_deliverable(_sig("TP2", mode="sniper"), flow) is False


# ---- sinyal lain tidak pernah digerbang ------------------------------------

def test_entry_and_hold_never_gated():
    flow = _flow(modes_with_position=())
    for kind in ("ENTRY_SELL", "ENTRY_BUY", "HOLD"):
        assert position_alert_deliverable(_sig(kind), flow) is True


# ---- OrderFlow.has_position ------------------------------------------------

def test_has_position_unknown_mode_is_false():
    flow = _flow(modes_with_position=("handgun",))
    assert flow.has_position("handgun") is True
    assert flow.has_position("sniper") is False
    assert flow.has_position("tidak-ada") is False
