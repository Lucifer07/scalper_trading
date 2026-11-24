"""Perpindahan simbol runtime: validasi, guard posisi, dan penerapan swap."""
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.config import load_settings  # noqa: E402
from xau_monitor.mt4_link import Mt4Rejected  # noqa: E402
from xau_monitor.runner import MonitorRunner  # noqa: E402
from xau_monitor.symbol_switch import (  # noqa: E402
    SymbolSwitch, normalize, open_tickets, settings_for,
)

ENV_KEYS = ("SYMBOL", "ORDER_ENABLED", "MT4_ALLOW_TRADING", "MT4_SYMBOL",
            "CONTROL_ENABLED", "CONTROL_HOST", "CONTROL_PORT", "LOG_FILE",
            "MT4_BRIDGE_PORT", "HISTORY_SOURCE", "COMMANDS_ENABLED")


class FakeLink:
    """Link bridge dengan tarif tetap: simbol dikenal/tidak, posisi n."""

    def __init__(self, symbols=("XAUUSD", "EURUSD"), positions=0):
        self.symbols = set(symbols)
        self.npos = positions
        self.calls = []

    def call(self, cmd, timeout=None, **params):
        self.calls.append((cmd, params.get("sym"), params.get("forming")))
        if cmd == "rates":
            if params.get("sym") not in self.symbols:
                raise Mt4Rejected("rates", {"err": "bad_symbol"})
            t = int(time.time())
            bar = f"{t},100.0,101.0,99.0,100.5,10"
            return {"sym": params["sym"], "count": "1", "gmt": "0",
                    "bars": bar}
        raise Mt4Rejected(cmd, {"err": "unknown"})

    def positions(self, symbol, magic):
        self.calls.append(("positions", symbol, None))
        return [{"ticket": i + 1} for i in range(self.npos)]


class FakeFeed:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.tick_count = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_runner(monkeypatch, **env):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return MonitorRunner(load_settings(""))


def fake_factory(settings, link, outq):
    return FakeFeed()


# ---- pure -----------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (" eurusd ", "EURUSD"), ("us100", "US100"), ("XAUUSD.m", "XAUUSD.M"),
    ("", None), ("AB", None), ("X!", None), ("WAYTOOLONGSYMBOL", None),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_settings_for_replaces_both_symbols(monkeypatch):
    r = make_runner(monkeypatch)
    new = settings_for(r.settings, "EURUSD")
    assert new.symbol == "EURUSD"
    assert new.orders.symbol == "EURUSD"
    assert new.orders.magic == r.settings.orders.magic
    assert r.settings.symbol == "XAUUSD"          # asli tidak berubah


# ---- guard & validasi -----------------------------------------------
def test_set_symbol_rejects_bad_format(monkeypatch):
    r = make_runner(monkeypatch)
    r.link = FakeLink()
    ok, msg = r.switcher.set_symbol("kepanjanganbangetsymbolnya")
    assert not ok and "tidak valid" in msg


def test_set_symbol_same_symbol_is_noop(monkeypatch):
    r = make_runner(monkeypatch)
    r.link = FakeLink()
    ok, _msg = r.switcher.set_symbol("xauusd")
    assert ok
    assert r.switcher._pending is None


def test_set_symbol_unknown_symbol_rejected(monkeypatch):
    r = make_runner(monkeypatch)
    r.link = FakeLink(symbols=("XAUUSD",))
    ok, msg = r.switcher.set_symbol("GOLDI")
    assert not ok and "tidak tersedia" in msg


def test_set_symbol_refused_while_positions_open(monkeypatch):
    r = make_runner(monkeypatch, ORDER_ENABLED="1", MT4_ALLOW_TRADING="1")
    r.link = FakeLink(positions=1)
    ok, msg = r.switcher.set_symbol("EURUSD")
    assert not ok and "posisi" in msg


def test_open_tickets_collects_per_mode(monkeypatch):
    r = make_runner(monkeypatch)
    assert open_tickets(FakeLink(positions=2), r.settings) == [1, 2]


def test_set_symbol_without_link_rejected(monkeypatch):
    r = make_runner(monkeypatch)
    ok, msg = r.switcher.set_symbol("EURUSD")
    assert not ok and "bridge" in msg


# ---- penerapan ------------------------------------------------------
def test_drain_applies_switch(monkeypatch):
    r = make_runner(monkeypatch)
    r.link = FakeLink()
    r.switcher = SymbolSwitch(r, feed_factory=fake_factory)
    old_builder, old_engine = r.builder, r.engines
    r.outq.put(("XAUUSD", 1, 3900.0))

    ok, _msg = r.switcher.set_symbol("EURUSD")
    assert ok
    r.switcher.drain()

    assert r.settings.symbol == "EURUSD"
    assert r.settings.orders.symbol == "EURUSD"
    assert r.hist                                  # seed terisi
    assert r.builder is not old_builder            # state di-reset
    assert r.engines is not old_engine
    assert r.price == 0.0
    assert r.broker_ticks.started
    assert r.outq.empty()                          # tick simbol lama dibuang


def test_drain_aborts_when_seed_fails(monkeypatch):
    r = make_runner(monkeypatch)
    r.link = FakeLink(symbols=("XAUUSD",))        # EURUSD tak dikenal
    r.switcher = SymbolSwitch(r, feed_factory=fake_factory)

    ok, _msg = r.switcher.set_symbol("EURUSD")     # probe ikut ditolak
    assert not ok
    r.switcher._pending = "EURUSD"                 # paksa antrean (seed gagal)
    r.switcher.drain()
    assert r.settings.symbol == "XAUUSD"           # tetap utuh
    assert r.broker_ticks is None
