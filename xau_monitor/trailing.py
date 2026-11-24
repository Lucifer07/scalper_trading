"""Manajemen order: pending limit -> posisi -> trailing TP.

Keputusan pemilik (2026-11-19):
- Entry = pending LIMIT di level stop engine; SL/TP broker dikosongkan.
- Pending kedaluwarsa setelah `ttl` detik (default 300) -> dibatalkan.
- Setelah terisi: trailing TP. Bila profit mencapai puncak lalu retrace
  >= `retrace` (default 20%) dari puncak, posisi ditutup.
- Tidak ada SL broker dan tidak ada reversal 45% (dibatalkan pemilik).

Semua profit dihitung dalam satuan harga (bukan uang) agar persentase retrace
tidak bergantung pada nilai per poin tiap simbol.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .mt4_link import Mt4Error, Mt4Link, Mt4Rejected

log = logging.getLogger("trailing")

POLL_TIMEOUT_S = 2.0


@dataclass
class PendingLimit:
    ticket: int
    side: str
    lots: float
    price: float
    magic: int
    mode: str
    created: float


@dataclass
class ActivePosition:
    ticket: int
    side: str
    lots: float
    magic: int
    peak: float = 0.0          # puncak profit dalam satuan harga


class Trailer:
    """State machine pending -> active -> closed untuk satu magic."""

    def __init__(self, link: Mt4Link, symbol: str, magic: int, audit,
                 ttl_s: float = 300.0, retrace: float = 0.20) -> None:
        self._link = link
        self._symbol = symbol
        self._magic = magic
        self._audit = audit
        self._ttl = float(ttl_s)
        self._retrace = float(retrace)
        self._pending: dict[int, PendingLimit] = {}
        self._active: dict[int, ActivePosition] = {}
        self._lock = threading.Lock()
        self._adopted = False
        self._pending_synced = False

    # ---- API -------------------------------------------------------
    def track(self, *, ticket: int, side: str, lots: float, price: float,
              mode: str) -> None:
        with self._lock:
            self._pending[ticket] = PendingLimit(
                ticket, side, lots, price, self._magic, mode, time.time())

    def has_active(self) -> bool:
        """Ada posisi terbuka yang sedang di-trail? (thread-safe, tanpa I/O).

        Pending yang belum terisi tidak dihitung — hanya posisi nyata.
        """
        with self._lock:
            return bool(self._active)

    def poll(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        msgs: list[str] = []
        try:
            positions = {int(p["ticket"]): p for p in
                         self._link.positions(self._symbol, self._magic,
                                              timeout=POLL_TIMEOUT_S)}
        except Mt4Error as exc:
            log.debug("poll posisi gagal: %s", exc)
            return msgs
        with self._lock:
            self._adopt(positions)
            self._sync_pending(msgs)
            self._check_filled(positions, msgs)
            self._expire_pending(positions, msgs, now)
            self._apply_trailing(positions, msgs)
        return msgs

    # ---- langkah ---------------------------------------------------
    def _adopt(self, positions: dict) -> None:
        """Posisi lama (mis. setelah restart) ikut di-trail mulai sekarang."""
        if self._adopted:
            return
        for ticket, pos in positions.items():
            self._active.setdefault(ticket, ActivePosition(
                ticket, str(pos["type"]).upper(), float(pos["lots"]),
                self._magic, 0.0))
        self._adopted = True

    def _sync_pending(self, msgs: list[str]) -> None:
        """Pending yatim (mis. setelah monitor restart) dibatalkan saat start.

        Tanpa ini pending lama bisa terisi tanpa trailing karena proses baru
        tidak punya state-nya.
        """
        if self._pending_synced:
            return
        try:
            tickets = self._link.pending_tickets(self._symbol, self._magic,
                                                 timeout=POLL_TIMEOUT_S)
        except Mt4Error as exc:
            log.debug("pending_list gagal: %s", exc)
            return          # EA belum siap: coba lagi di poll berikutnya
        self._pending_synced = True
        for ticket in tickets:
            if ticket in self._pending:
                continue
            try:
                self._link.order_delete(ticket, timeout=POLL_TIMEOUT_S)
                msgs.append(f"🧹 <b>PENDING YATIM DIBATALKAN</b> ticket #{ticket}")
                self._audit({"event": "pending_orphan_deleted", "ticket": ticket})
            except Mt4Error as exc:
                log.debug("hapus pending yatim #%s gagal: %s", ticket, exc)

    def _check_filled(self, positions: dict, msgs: list[str]) -> None:
        for ticket, p in list(self._pending.items()):
            if ticket not in positions:
                continue
            del self._pending[ticket]
            self._active[ticket] = ActivePosition(
                ticket, p.side, p.lots, p.magic, 0.0)
            msgs.append(f"✅ <b>LIMIT TERISI</b> {p.side} {p.lots:g} lot "
                        f"@ {p.price:.{self._digits()}} · ticket #{ticket}")
            self._audit({"event": "filled", "ticket": ticket,
                         "side": p.side, "price": p.price})

    def _expire_pending(self, positions: dict, msgs: list[str],
                        now: float) -> None:
        for ticket, p in list(self._pending.items()):
            if now - p.created < self._ttl:
                continue
            del self._pending[ticket]
            try:
                self._link.order_delete(ticket, timeout=POLL_TIMEOUT_S)
            except Mt4Rejected as exc:
                if "is_position" in str(exc) and ticket in positions:
                    self._active[ticket] = ActivePosition(
                        ticket, p.side, p.lots, p.magic, 0.0)
                    msgs.append(f"✅ <b>LIMIT TERISI</b> {p.side} "
                                f"{p.lots:g} lot · ticket #{ticket}")
                    self._audit({"event": "filled", "ticket": ticket,
                                 "side": p.side, "price": p.price})
                else:
                    self._audit({"event": "pending_gone", "ticket": ticket,
                                 "detail": str(exc)})
            except Mt4Error as exc:
                log.warning("batal pending #%s gagal: %s", ticket, exc)
            else:
                mins = self._ttl / 60.0
                msgs.append(f"⌛ <b>LIMIT DIBATALKAN</b> {p.side} {p.lots:g} lot "
                            f"@ {p.price:.{self._digits()}} — tidak tersentuh "
                            f"{mins:g} menit")
                self._audit({"event": "pending_expired", "ticket": ticket,
                             "side": p.side, "price": p.price})

    def _apply_trailing(self, positions: dict, msgs: list[str]) -> None:
        if not self._active:
            return
        try:
            info = self._link.symbol_info(self._symbol, timeout=POLL_TIMEOUT_S)
        except Mt4Error as exc:
            log.debug("symbol_info gagal: %s", exc)
            return
        bid = float(info.get("bid", 0) or 0)
        ask = float(info.get("ask", 0) or 0)
        point = float(info.get("point", 0) or 0)
        spread = float(info.get("spread", 0) or 0) * point
        min_peak = 2.0 * spread       # abaikan puncak yang belum menutup spread
        if bid <= 0 or ask <= 0:
            return
        for ticket, a in list(self._active.items()):
            pos = positions.get(ticket)
            if pos is None:
                del self._active[ticket]
                continue
            entry = float(pos["open"])
            profit = (bid - entry) if a.side == "BUY" else (entry - ask)
            a.peak = max(a.peak, profit)
            if a.peak < min_peak or profit > a.peak * (1.0 - self._retrace):
                continue
            try:
                self._link.close_position(ticket, a.lots, timeout=POLL_TIMEOUT_S)
            except Mt4Error as exc:
                log.warning("trail close #%s gagal: %s", ticket, exc)
                continue
            del self._active[ticket]
            msgs.append(f"🎯 <b>TP TRAILING</b> {a.side} {a.lots:g} lot · "
                        f"retrace {self._retrace:.0%} dari puncak "
                        f"{a.peak:.{self._digits()}} · ticket #{ticket}")
            self._audit({"event": "trail_close", "ticket": ticket,
                         "side": a.side, "peak": a.peak, "profit": profit})

    def _digits(self) -> int:
        try:
            info = self._link.symbol_info(self._symbol, timeout=POLL_TIMEOUT_S)
            return int(info.get("digits", 2) or 2)
        except (Mt4Error, ValueError):
            return 2
