"""Tests for the MT4 link (real sockets against the simulator EA)."""
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from tools.fake_ea import FakeEa  # noqa: E402
from xau_monitor.mt4_link import (  # noqa: E402
    Mt4Link, Mt4Rejected, Mt4Unavailable,
)


def wait_connected(link: Mt4Link, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if link.connected:
            return
        time.sleep(0.02)
    raise AssertionError("simulator EA tidak terhubung")


@pytest.fixture
def bridge():
    link = Mt4Link(0, call_timeout=1.0)
    ea = FakeEa(link.port).start()
    wait_connected(link)
    yield link, ea
    ea.stop()
    link.shutdown()


def test_ping_and_symbol_info(bridge):
    link, _ea = bridge
    ping = link.ping()
    assert ping["login"] == "2098919075"
    assert ping["server"] == "FakeBroker-Demo"
    assert ping["trading_allowed"] == "1"

    info = link.symbol_info("XAUUSD")
    assert float(info["bid"]) == pytest.approx(2000.0)
    assert float(info["ask"]) == pytest.approx(2000.5)
    assert float(info["lotstep"]) == pytest.approx(0.01)


def test_positions_empty_and_after_order(bridge):
    link, ea = bridge
    assert link.positions("XAUUSD", ea.magic) == []
    resp = link.order_send(side="sell", symbol="XAUUSD", lots=0.1, sl=2005.0,
                           tp=1990.0, magic=ea.magic, deviation=30, comment="t")
    assert resp["ticket"] == "9001"
    rows = link.positions("XAUUSD", ea.magic)
    assert len(rows) == 1
    assert rows[0]["type"] == "sell"
    assert rows[0]["sl"] == pytest.approx(2005.0)
    assert rows[0]["comment"] == "t"


def test_close_and_modify(bridge):
    link, ea = bridge
    ticket = int(link.order_send(side="buy", symbol="XAUUSD", lots=0.1, sl=1995.0,
                                 tp=2010.0, magic=ea.magic, deviation=30,
                                 comment="t")["ticket"])
    mod = link.modify(ticket, 1996.0, 2011.0)
    assert float(mod["sl"]) == pytest.approx(1996.0)
    closed = link.close_position(ticket, 0.1)
    assert closed["ticket"] == str(ticket)
    assert link.positions("XAUUSD", ea.magic) == []


def test_ea_rejection_raises(bridge):
    link, ea = bridge
    ea.allow_trading = False
    with pytest.raises(Mt4Rejected) as exc:
        link.order_send(side="sell", symbol="XAUUSD", lots=0.1, sl=2005.0,
                        tp=1990.0, magic=ea.magic, deviation=30, comment="t")
    assert exc.value.code == "trading_disabled"
    assert ea.sent_orders == []


def test_unknown_symbol_raises(bridge):
    link, _ea = bridge
    with pytest.raises(Mt4Rejected) as exc:
        link.symbol_info("EURUSD")
    assert exc.value.code == "unknown_symbol"


def test_timeout_raises_unavailable():
    link = Mt4Link(0, call_timeout=0.3)
    ea = FakeEa(link.port).start()
    wait_connected(link)
    ea.silent = True
    try:
        with pytest.raises(Mt4Unavailable):
            link.ping()
    finally:
        ea.stop()
        link.shutdown()


def test_call_without_ea_raises():
    link = Mt4Link(0, call_timeout=0.3)
    try:
        with pytest.raises(Mt4Unavailable):
            link.ping()
    finally:
        link.shutdown()


def test_disconnect_unblocks_pending_and_marks_down(bridge):
    link, ea = bridge
    ea.stop()
    deadline = time.time() + 3.0
    while time.time() < deadline and link.connected:
        time.sleep(0.02)
    assert not link.connected
    with pytest.raises(Mt4Unavailable):
        link.ping()
