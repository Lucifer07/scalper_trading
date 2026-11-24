"""Test manajemen order: pending limit + trailing TP 20% (tanpa reversal)."""
import sys
import time

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.mt4_link import Mt4Rejected  # noqa: E402
from xau_monitor.trailing import Trailer  # noqa: E402

MAGIC = 42


class FakeLink:
    def __init__(self):
        self.bid, self.ask = 2000.0, 2000.5
        self.point, self.spread, self.digits = 0.01, 20, 2
        self.book: dict[int, dict] = {}      # posisi terbuka
        self.deleted: list[int] = []
        self.closed: list[int] = []
        self.delete_error: str | None = None
        self.pending: list[int] = []
        self.fail_pending = False

    def positions(self, symbol, magic, timeout=None):
        return [p for p in self.book.values() if p["magic"] == magic]

    def symbol_info(self, symbol, timeout=None):
        return {"bid": self.bid, "ask": self.ask, "point": self.point,
                "spread": self.spread, "digits": self.digits}

    def pending_tickets(self, symbol, magic, timeout=None):
        if self.fail_pending:
            raise Mt4Rejected("pending_list", {"err": "ea_down"})
        return list(self.pending)

    def order_delete(self, ticket, timeout=None):
        if self.delete_error:
            raise Mt4Rejected("order_delete", {"err": self.delete_error})
        self.deleted.append(ticket)
        return {"ticket": ticket}

    def close_position(self, ticket, lots, timeout=None):
        self.closed.append(ticket)
        self.book.pop(ticket, None)
        return {"ticket": ticket}


def _pos(ticket: int, side: str, open_: float, magic: int = MAGIC) -> dict:
    return {"ticket": ticket, "type": side, "lots": 0.1, "open": open_,
            "sl": 0.0, "tp": 0.0, "profit": 0.0, "comment": "", "magic": magic}


def _trailer(link: FakeLink) -> Trailer:
    return Trailer(link, "XAUUSD", MAGIC, lambda row: None,
                   ttl_s=300.0, retrace=0.20)


def test_pending_expires_after_ttl():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=1, side="SELL", lots=0.1, price=2005.0, mode="sniper")
    msgs = tr.poll(now=time.time() + 301)
    assert link.deleted == [1]
    assert any("DIBATALKAN" in m for m in msgs)


def test_orphan_pending_deleted_on_first_poll():
    # Pending sisa proses lama tidak dikenal -> wajib dibatalkan saat start.
    link = FakeLink()
    link.pending = [77]
    tr = _trailer(link)
    msgs = tr.poll(now=time.time())
    assert link.deleted == [77]
    assert any("YATIM" in m for m in msgs)


def test_pending_sync_retries_when_ea_not_ready():
    link = FakeLink()
    link.pending = [88]
    link.fail_pending = True
    tr = _trailer(link)
    tr.poll(now=time.time())          # EA belum siap -> jangan tandai selesai
    assert link.deleted == []
    link.fail_pending = False
    tr.poll(now=time.time())          # percobaan kedua berhasil
    assert link.deleted == [88]


def test_pending_still_valid_before_ttl():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=1, side="SELL", lots=0.1, price=2005.0, mode="sniper")
    assert tr.poll(now=time.time() + 10) == []
    assert link.deleted == []


def test_pending_fill_becomes_position():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=2, side="SELL", lots=0.1, price=2005.0, mode="sniper")
    link.book[2] = _pos(2, "SELL", 2005.0)
    msgs = tr.poll(now=time.time())
    assert any("TERISI" in m for m in msgs)
    assert tr._active.get(2) is not None


def test_trailing_tp_at_20pct_of_peak():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=3, side="SELL", lots=0.1, price=2000.5, mode="handgun")
    link.book[3] = _pos(3, "SELL", 2000.5)
    tr.poll(now=time.time())                 # fill -> active
    link.ask = 1999.5                        # profit 1.0 (puncak)
    tr.poll(now=time.time())
    link.ask = 1999.71                       # profit 0.79 <= 80% * 1.0 -> TP
    msgs = tr.poll(now=time.time())
    assert link.closed == [3]
    assert any("TP TRAILING" in m for m in msgs)


def test_trailing_ignores_peak_below_spread():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=4, side="SELL", lots=0.1, price=2000.5, mode="handgun")
    link.book[4] = _pos(4, "SELL", 2000.5)
    tr.poll(now=time.time())
    link.ask = 2000.2                        # profit 0.3 < 2*spread = 0.40
    tr.poll(now=time.time())
    link.ask = 2000.45                       # profit kecil, retrace besar
    tr.poll(now=time.time())
    assert link.closed == []                 # belum layak trail


def test_delete_fails_because_filled_is_treated_as_position():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=5, side="BUY", lots=0.1, price=1995.0, mode="sniper")
    link.book[5] = _pos(5, "BUY", 1995.0)
    link.delete_error = "is_position"
    msgs = tr.poll(now=time.time() + 301)
    assert any("TERISI" in m for m in msgs)
    assert tr._active.get(5) is not None


def test_existing_position_is_adopted_not_reversed():
    link = FakeLink()
    tr = _trailer(link)
    link.book[6] = _pos(6, "BUY", 1990.0)
    tr.poll(now=time.time())                 # adopt, tanpa pending
    link.bid = 1992.0                        # profit 2.0
    tr.poll(now=time.time())
    link.bid = 1991.5                        # profit 1.5 <= 80% -> close (bukan reverse)
    tr.poll(now=time.time())
    assert link.closed == [6]
    assert not hasattr(link, "opened")       # tidak ada order lawan


def test_has_active_false_while_pending_only():
    # Pending belum terisi bukan posisi -> alert TP harus tetap dibungkam.
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=7, side="BUY", lots=0.1, price=1995.0, mode="sniper")
    tr.poll(now=time.time() + 10)
    assert tr.has_active() is False


def test_has_active_true_after_fill_false_after_gone():
    link = FakeLink()
    tr = _trailer(link)
    tr.track(ticket=8, side="BUY", lots=0.1, price=2000.0, mode="sniper")
    link.book[8] = _pos(8, "BUY", 2000.0)
    tr.poll(now=time.time())
    assert tr.has_active() is True
    del link.book[8]                     # posisi hilang (trail close / manual)
    tr.poll(now=time.time())
    assert tr.has_active() is False
