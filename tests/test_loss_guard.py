"""Test hard loss stop: baseline, konfirmasi sampel, flatten, latch, stop app."""
import json
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.loss_guard import GuardConfig, LossGuard  # noqa: E402
from xau_monitor.mt4_link import Mt4Rejected  # noqa: E402
from xau_monitor.orders import AccountState  # noqa: E402

BASE_MAGIC = 100


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry flatten memakai sleep nyata; test tidak perlu menunggu."""
    monkeypatch.setattr("xau_monitor.loss_guard.time.sleep", lambda _s: None)


class FakeLink:
    def __init__(self, equity: float = 100.0, balance: float = 100.0) -> None:
        self.equity, self.balance = equity, balance
        self.book: dict[tuple[str, int], list[dict]] = {}
        self.pending: dict[tuple[str, int], list[int]] = {}
        self.closed: list[int] = []
        self.deleted: list[int] = []
        self.ping_error = False
        self.close_error = False

    def ping(self):
        if self.ping_error:
            raise OSError("EA hilang")
        return {"login": "1", "server": "FBS", "currency": "USD",
                "equity": f"{self.equity:.2f}", "balance": f"{self.balance:.2f}",
                "freemargin": "10.00", "leverage": "1000"}

    def positions(self, symbol, magic, timeout=None):
        return list(self.book.get((symbol.upper(), magic), []))

    def pending_tickets(self, symbol, magic, timeout=None):
        return list(self.pending.get((symbol.upper(), magic), []))

    def order_delete(self, ticket, timeout=None):
        self.deleted.append(ticket)
        return {"ticket": ticket}

    def close_position(self, ticket, lots, timeout=None):
        if self.close_error:
            raise Mt4Rejected("order_close", {"err": "132",
                                              "msg": "market is closed"})
        self.closed.append(ticket)
        for book in self.book.values():
            book[:] = [p for p in book if p["ticket"] != ticket]
        return {"ticket": ticket}


def _account(equity: float, balance: float) -> AccountState:
    return AccountState.from_wire({"login": "1", "equity": f"{equity:.2f}",
                                   "balance": f"{balance:.2f}",
                                   "freemargin": "10.00", "leverage": "1000"})


def _pos(ticket: int, lots: float = 0.01) -> dict:
    return {"ticket": ticket, "type": "BUY", "lots": lots, "open": 3600.0,
            "sl": 0.0, "tp": 0.0, "profit": -1.0, "comment": ""}


def _cfg(tmp_path, **kw) -> GuardConfig:
    defaults = dict(limit_usd=8.0, poll_s=1.0, confirm=1,
                    state_path=str(tmp_path / "state/loss_guard.json"),
                    latch_path=str(tmp_path / "state/trading_locked.json"),
                    lock_path=str(tmp_path / "state/loss_guard.lock"),
                    symbols_path=str(tmp_path / "symbols.json"),
                    stop_units=("unit-a.service",))
    defaults.update(kw)
    return GuardConfig(**defaults)


def _guard(tmp_path, link, stopper, clock=None, **kw) -> LossGuard:
    return LossGuard(_cfg(tmp_path, **kw), link, None, symbol="XAUUSD",
                     magic=BASE_MAGIC, stopper=stopper,
                     clock=clock or time.time)


class Clock:
    """Jam palsu: poll_s tidak boleh membuat test tidur sungguhan."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def test_baseline_written_and_no_trip_above_limit(tmp_path):
    link = FakeLink(equity=92.01, balance=100.0)
    guard = _guard(tmp_path, link, lambda: None)

    assert guard.check() is False
    state = json.loads((tmp_path / "state/loss_guard.json").read_text())
    assert state["baseline_balance"] == 100.0
    assert not (tmp_path / "state/trading_locked.json").exists()


def test_trip_closes_positions_cancels_pending_and_stops(tmp_path):
    (tmp_path / "symbols.json").write_text(json.dumps({
        "XAUUSD": {"magic": BASE_MAGIC}, "US100": {"magic": 900},
    }))
    link = FakeLink(equity=91.0, balance=100.0)
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    link.book[("XAUUSD", BASE_MAGIC + 1)] = [_pos(2)]
    link.book[("US100", 900)] = [_pos(3, lots=0.05)]
    link.pending[("XAUUSD", BASE_MAGIC)] = [11]

    calls: list[int] = []
    guard = _guard(tmp_path, link, lambda: calls.append(1))

    assert guard.check() is True
    assert sorted(link.closed) == [1, 2, 3]
    assert link.deleted == [11]
    assert calls == [1]
    assert guard.locked is True
    latch = json.loads((tmp_path / "state/trading_locked.json").read_text())
    assert latch["limit"] == 8.0 and latch["loss"] == 9.0


def test_confirm_samples_required_before_action(tmp_path):
    link = FakeLink(equity=90.0, balance=100.0)
    calls: list[int] = []
    clock = Clock()
    guard = _guard(tmp_path, link, lambda: calls.append(1), confirm=2,
                   clock=clock)

    assert guard.check() is False          # sampel 1: tunggu
    assert calls == []
    clock.tick(5)
    assert guard.check() is True           # sampel 2: eksekusi
    assert calls == [1]


def test_latch_blocks_action_after_restart(tmp_path):
    link = FakeLink(equity=90.0, balance=100.0)
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    calls: list[int] = []
    assert _guard(tmp_path, link, lambda: calls.append(1)).check() is True

    link2 = FakeLink(equity=90.0, balance=100.0)
    link2.book[("XAUUSD", BASE_MAGIC)] = [_pos(9)]
    guard2 = _guard(tmp_path, link2, lambda: calls.append(1))
    assert guard2.locked is True
    assert guard2.check() is True
    assert link2.closed == [] and calls == [1]


def test_missing_registry_falls_back_to_own_symbol(tmp_path):
    link = FakeLink(equity=90.0, balance=100.0)
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    guard = _guard(tmp_path, link, lambda: None)
    assert guard.check() is True
    assert link.closed == [1]


def test_ping_failure_is_not_a_trigger(tmp_path):
    link = FakeLink()
    link.ping_error = True
    guard = _guard(tmp_path, link, lambda: None)
    assert guard.check() is False
    assert not (tmp_path / "state/trading_locked.json").exists()


def test_failed_close_keeps_app_alive_then_retries(tmp_path):
    """EA menolak close (mis. market tutup): app TIDAK boleh mati meninggalkan posisi."""
    link = FakeLink(equity=90.0, balance=100.0)
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    link.close_error = True
    clock = Clock()
    calls: list[int] = []
    guard = _guard(tmp_path, link, lambda: calls.append(1), clock=clock)

    assert guard.check() is False        # belum tuntas -> jangan stop app
    assert guard.locked is True
    assert calls == []
    assert (tmp_path / "state/trading_locked.json").exists()

    link.close_error = False
    clock.tick(10)
    assert guard.check() is True         # retry berhasil -> baru matikan
    assert link.closed == [1]
    assert calls == [1]


def test_boundary_minus_7_99_does_nothing(tmp_path):
    """Batas keras: 7.99 rugi TIDAK boleh menyentuh posisi sama sekali."""
    link = FakeLink(equity=92.01, balance=100.0)     # 100 - 92.01 = 7.99
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    calls: list[int] = []
    guard = _guard(tmp_path, link, lambda: calls.append(1), confirm=1)

    assert guard.check() is False
    assert link.closed == [] and calls == []
    assert not (tmp_path / "state/trading_locked.json").exists()


def test_boundary_exactly_minus_8_triggers(tmp_path):
    link = FakeLink(equity=92.0, balance=100.0)      # 100 - 92 = 8.00
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    calls: list[int] = []
    guard = _guard(tmp_path, link, lambda: calls.append(1), confirm=1)

    assert guard.check() is True
    assert link.closed == [1] and calls == [1]


def test_snapshot_logger_never_acts(tmp_path):
    """Regresi insiden 05:00: logger PnL pernah berisi body _trip."""
    link = FakeLink(equity=90.0, balance=100.0)
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    guard = _guard(tmp_path, link, lambda: (_ for _ in ()).throw(AssertionError))
    account = _account(equity=90.0, balance=100.0)

    guard._log_snapshot(account, 100.0, 10.0)

    assert link.closed == []
    assert guard.locked is False
    assert not (tmp_path / "state/trading_locked.json").exists()


def test_trip_refuses_below_limit_even_if_called_directly(tmp_path):
    """Pagar kedua: _trip tidak boleh menutup posisi di rugi < limit."""
    link = FakeLink(equity=97.91, balance=100.0)     # rugi 2.09
    link.book[("XAUUSD", BASE_MAGIC)] = [_pos(1)]
    calls: list[int] = []
    guard = _guard(tmp_path, link, lambda: calls.append(1))

    assert guard._trip(_account(97.91, 100.0), 100.0, 2.09) is False
    assert link.closed == [] and calls == []
    assert not (tmp_path / "state/trading_locked.json").exists()
