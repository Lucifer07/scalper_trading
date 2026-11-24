"""Perpindahan simbol runtime tanpa restart.

Permintaan datang dari control API (thread HTTP) dan hanya mengantre;
penerapannya terjadi di loop utama (`drain`) sehingga tidak ada mutasi state
monitor lintas thread.

Urutan penerapan sengaja fail-closed: seed history dan pembangunan jalur order
untuk simbol BARU dieksekusi lebih dulu — bila gagal, monitor tetap utuh di
simbol lama, tidak ada keadaan setengah-pindah.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import replace

from .advisor import AdvisorUnavailable
from .alerts import FeedWatch
from .candles import M1Builder
from .config import Settings
from .feed_mt4 import SeedError
from .mt4_link import Mt4Link, Mt4Rejected, Mt4Unavailable
from .order_flow import OrderFlow
from .pipeline import deliver
from .strategy import Engine
from .wiring import build_feeds, seed_history

log = logging.getLogger("symbol")

SYMBOL_RE = re.compile(r"^[A-Z0-9._ -]{3,12}$")


def normalize(raw: str) -> str | None:
    """Normalisasi input simbol; None bila format tidak layak."""
    sym = (raw or "").strip().upper()
    return sym if SYMBOL_RE.match(sym) else None


def settings_for(s: Settings, symbol: str) -> Settings:
    """Settings baru dengan simbol feed dan simbol broker diganti bersamaan."""
    return replace(s, symbol=symbol, orders=replace(s.orders, symbol=symbol))


def probe(link: Mt4Link, symbol: str) -> str:
    """Cek simbol dikenal broker lewat bar M1. String kosong = OK."""
    try:
        resp = link.call("rates", sym=symbol, tf="M1", count=2, forming=1,
                         timeout=8.0)
    except (Mt4Unavailable, Mt4Rejected, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if not resp.get("bars"):
        return "broker tidak mengirim bar M1"
    return ""


def open_tickets(link: Mt4Link, s: Settings) -> list[int]:
    """Tiket posisi terbuka milik magic monitor ini (guard anti-yatim)."""
    tickets: list[int] = []
    for mode in s.modes:
        for pos in link.positions(s.orders.symbol, s.orders.magic_for(mode)):
            tickets.append(int(pos.get("ticket", 0)))
    return [t for t in tickets if t > 0]


def _purge_foreign(q, keep: str) -> None:
    """Buang tick simbol lama yang tertinggal di antrean (API publik Queue)."""
    kept = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item[0] == keep:
            kept.append(item)
    for item in kept:
        q.put(item)


class SymbolSwitch:
    """Antrean perpindahan simbol; dipasang di MonitorRunner."""

    def __init__(self, runner, feed_factory=None) -> None:
        self._runner = runner
        self._feed_factory = feed_factory or build_feeds
        self._lock = threading.Lock()
        self._pending: str | None = None

    # ---- dari control API (thread HTTP) ----------------------------
    def set_symbol(self, raw: str) -> tuple[bool, str]:
        r = self._runner
        sym = normalize(raw)
        if sym is None:
            return False, f"simbol tidak valid: {raw!r} (3-12 huruf/angka/titik)"
        if sym == r.settings.symbol:
            return True, f"monitor sudah aktif di {sym}"
        if r.link is None:
            return False, ("bridge MT4 tidak aktif — perpindahan simbol butuh "
                           "history broker (HISTORY_SOURCE=mt4)")
        if r.settings.orders.armed:
            try:
                tickets = open_tickets(r.link, r.settings)
            except (Mt4Unavailable, Mt4Rejected, ValueError) as exc:
                return False, f"posisi tidak bisa diverifikasi: {exc}"
            if tickets:
                return False, f"masih ada posisi terbuka {tickets} — tutup dulu"
        err = probe(r.link, sym)
        if err:
            return False, f"{sym} tidak tersedia di broker: {err}"
        with self._lock:
            self._pending = sym
        return True, f"perpindahan ke {sym} diproses (berlaku ≤1 detik)"

    # ---- dari loop utama -------------------------------------------
    def drain(self) -> None:
        with self._lock:
            sym = self._pending
            self._pending = None
        if sym:
            self._apply(sym)

    def _apply(self, sym: str) -> None:
        r = self._runner
        old = r.settings.symbol
        new_settings = settings_for(r.settings, sym)
        try:
            hist = seed_history(new_settings, r.link, None)
            if not hist:
                raise SeedError(f"{sym}: seed history kosong")
        except SeedError as exc:
            log.error("pindah %s -> %s gagal (seed): %s — tetap di %s",
                      old, sym, exc, old)
            deliver(r.notifier, f"⚠️ Pindah {old} → {sym} gagal: {exc}")
            return
        new_orders = None
        if new_settings.orders.enabled:
            try:
                new_orders = OrderFlow(new_settings, r.link)
                new_orders.build()
            except AdvisorUnavailable as exc:
                log.error("pindah %s -> %s gagal (order): %s — tetap di %s",
                          old, sym, exc, old)
                deliver(r.notifier, f"⚠️ Pindah {old} → {sym} gagal: {exc}")
                return
        if r.broker_ticks is not None:
            r.broker_ticks.stop()
        if r.orders is not None:
            r.orders.shutdown()

        r.settings = new_settings
        r.orders = new_orders
        r.builder = M1Builder()
        r.engines = {m: Engine(new_settings.cooldown_minutes, m,
                               digits=r.digits,
                               limit_offset_pct=new_settings.orders.limit_offset_pct,
                               limit_offset_max_pips=new_settings.orders.limit_offset_max_pips)
                     for m in new_settings.modes}
        r.hist = hist
        r.snaps = {}
        r.forming = None
        r.price = 0.0
        r.watch = FeedWatch(r.notifier, new_settings)
        r._last_seed_refresh = time.time()  # noqa: SLF001 — state milik runner
        r.broker_ticks = self._feed_factory(new_settings, r.link, r.outq)
        r.broker_ticks.start()
        _purge_foreign(r.outq, sym)
        log.warning("simbol %s -> %s (state di-reset, seed %d TF)",
                    old, sym, len(hist))
        deliver(r.notifier, f"🔄 <b>{old} → {sym}</b>\n"
                            "monitor pindah simbol — state engine di-reset, "
                            "history di-seed ulang.")
