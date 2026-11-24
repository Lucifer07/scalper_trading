"""Watchdog feed MT4 + heartbeat Telegram. Timer disimpan di objek, bukan global.

Feed kantor (FIX worker) dipensiunkan: satu-satunya sumber tick adalah bar M1
native broker lewat EA bridge, jadi watchdog mengawasi umur tick MT4.
"""
from __future__ import annotations

import html
import logging
import time

from . import report
from .config import Settings
from .pipeline import deliver

log = logging.getLogger("alerts")
STALE_WARN_S = 120
STALE_LOG_EVERY_S = 60
STALE_ALERT_EVERY_S = 900


class FeedWatch:
    def __init__(self, notifier, settings: Settings) -> None:
        self.notifier = notifier
        self.settings = settings
        self.last_log = 0.0
        self.last_alert = 0.0
        self.last_heartbeat = time.time()

    def check(self, broker, started_at: float) -> None:
        """Log staleness berkala + satu alert Telegram per 15 menit."""
        if broker is None:
            return
        stale = broker.age
        now = time.time()
        if stale > STALE_WARN_S and now - self.last_log > STALE_LOG_EVERY_S:
            self.last_log = now
            log.warning("tick MT4 stale %.0fs — EA bridge/terminal mungkin putus", stale)
        if not self.notifier or now - self.last_alert < STALE_ALERT_EVERY_S:
            return
        symbol = html.escape(self.settings.symbol)
        if broker.tick_count == 0 and now - started_at > 300:
            self.last_alert = now
            deliver(self.notifier,
                    f"⚠️ <b>Feed {symbol} belum menerima tick MT4</b> sejak start — "
                    "cek EA bridge (terminal MT4 jalan? simbol ada di Market Watch?)")
        elif broker.tick_count > 0 and stale > 180:
            self.last_alert = now
            deliver(self.notifier,
                    f"⚠️ <b>Feed {symbol} berhenti</b>\n"
                    f"Tick MT4 tidak masuk {stale / 60:.0f} menit — cek EA bridge.\n"
                    f"Monitor tetap jalan, sinyal tertahan sampai feed pulih.")

    def heartbeat(self, *, price: float, engines: dict, broker, started_at: float) -> None:
        s = self.settings
        if not (s.heartbeat_minutes > 0 and self.notifier and price):
            return
        if time.time() - self.last_heartbeat <= s.heartbeat_minutes * 60:
            return
        self.last_heartbeat = time.time()
        uptime = time.time() - started_at
        rate = broker.tick_count / max(uptime, 1.0) if broker else 0.0
        deliver(self.notifier, report.fmt_status_all(s.symbol, price, engines, broker,
                                                     rate, uptime))
