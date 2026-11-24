"""Perakitan objek runtime: bridge MT4, feed, control API.

Dipisah dari runner agar lifecycle loop tetap terbaca dan wiring mudah diuji
tanpa menjalankan monitor penuh.
"""
from __future__ import annotations

import logging

from .config import Settings
from .control import ControlServer
from .feed_mt4 import SeedError
from .feed_mt4 import seed as mt4_seed
from .feed_mt4_ticks import Mt4BarTicks
from .feed_yahoo import seed as yahoo_seed
from .mt4_link import Mt4Link

log = logging.getLogger("wiring")


class FeedConfigError(RuntimeError):
    """Konfigurasi feed tidak bisa dipenuhi (mis. bridge MT4 tidak tersedia)."""


def open_link(settings: Settings) -> Mt4Link:
    o = settings.orders
    return Mt4Link(o.port, host=o.bridge_host, call_timeout=o.call_timeout)


def build_feeds(settings: Settings, link: Mt4Link | None, outq) -> Mt4BarTicks:
    """Buat (belum start) poller bar M1 MT4 — satu-satunya sumber tick."""
    if link is None:
        raise FeedConfigError(
            "bridge MT4 tidak tersedia — tick hanya boleh dari MT4 "
            "(nyalakan terminal + EA; cek MT4_BRIDGE_PORT / MT4_BRIDGE_HOST)")
    return Mt4BarTicks(link, settings.orders.symbol, outq, settings.tick_poll_ms)


def start_feeds(settings: Settings, link: Mt4Link | None, outq) -> Mt4BarTicks:
    broker = build_feeds(settings, link, outq)
    broker.start()
    log.warning("sumber tick: MT4 bar M1 (poll %d ms) — satu-satunya sumber",
                settings.tick_poll_ms)
    return broker


def seed_history(settings: Settings, link: Mt4Link | None,
                 price_ref: float | None) -> dict:
    """Utamakan bar broker (sumber eksekusi); Yahoo hanya fallback eksplisit."""
    s = settings
    if link is not None and s.history_source != "yahoo":
        log.info("seed TF besar dari MT4 %s ...", s.orders.symbol)
        try:
            return mt4_seed(link, s.orders.symbol)
        except SeedError as exc:
            if s.history_source == "mt4":
                raise
            log.error("seed MT4 gagal, jatuh ke Yahoo: %s", exc)
    if s.yahoo_context and price_ref:
        log.info("seed TF besar dari Yahoo %s ...", s.yahoo_symbol)
        return yahoo_seed(s.yahoo_symbol, price_ref)
    return {}


def start_control(settings: Settings, status_fn, report_fn,
                  symbol_fn=None, layering_fn=None) -> ControlServer | None:
    p = settings.panel
    if not p.control_enabled:
        log.warning("control API dimatikan (CONTROL_ENABLED=0) — panel GUI "
                    "tidak bisa memantau")
        return None
    try:
        server = ControlServer(p.control_host, p.control_port, status_fn,
                               report_fn, symbol_fn, layering_fn)
    except OSError as exc:
        log.error("control API %s:%d tidak bisa bind: %s — panel GUI tidak tersedia",
                  p.control_host, p.control_port, exc)
        return None
    return server if server.start() else None
