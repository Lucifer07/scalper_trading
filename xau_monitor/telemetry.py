"""Telemetri monitor: payload status JSON, laporan teks, pengumuman start.

Fungsi murni atas data yang sudah dihitung runner — tidak menyentuh feed atau
thread, sehingga mudah diuji dan dipakai control API.
"""
from __future__ import annotations

import html
import time

from . import readiness, report, status
from .candles import snapshot
from .config import Settings


def status_payload(settings, *, price: float, started_at: float, broker,
                   engines: dict, link, order_state: str,
                   readiness: dict | None = None, digits: int = 2,
                   layering: bool = False) -> dict:
    now = time.time()
    uptime = now - started_at if started_at else 0.0
    return status.payload(
        symbol=settings.symbol, price=price, uptime_s=uptime, digits=digits,
        tick=status.tick_payload(
            rate=(broker.tick_count / max(uptime, 1.0)) if broker else 0.0,
            mt4_ticks=broker.tick_count if broker else 0,
            mt4_age_s=broker.age if broker else 1e9),
        engines=status.engines_payload(engines),
        orders={"state": order_state, "armed": settings.orders.armed,
                "modes": list(settings.modes), "layering": layering,
                "layering_min": settings.orders.layering_min},
        broker={"connected": bool(link and link.connected),
                "symbol": settings.orders.symbol,
                "last_error": broker.last_error if broker else ""},
        dry_run=settings.dry_run, readiness=readiness)


def command_text(settings: Settings, cmd: str, *, price: float, snaps: dict,
                 hist: dict, engines: dict, forming, broker,
                 started_at: float, digits: int = 2) -> str:
    price_now = price
    seeded = False
    if not snaps:
        snaps, price_now = seed_view(hist, price)
        seeded = price_now > 0
    uptime = time.time() - started_at if started_at else 0.0
    rate = broker.tick_count / max(uptime, 1.0) if broker else 0.0
    if cmd == "status":
        text = report.fmt_status_all(settings.symbol, price_now, engines, broker,
                                     rate, uptime, digits=digits)
        age = broker.age if broker else None
        age_txt = "—" if not age or age >= 1e9 else f"{age:.0f}s"
        text += (f"\nTick MT4: {broker.tick_count if broker else 0}"
                 f" · umur {age_txt} · {rate:.0f}/s")
        if seeded:
            text += "\n⚠️ harga seed broker (feed live MT4 belum pulih)"
        return text
    if cmd == "planning":
        blocks = []
        for eng in engines.values():
            blocks.append(report.fmt_planning(
                settings.symbol, price_now, eng,
                eng.diagnose(snaps, price_now, now=time.time(),
                             forming_m15=forming), digits=digits))
        text = "\n\n".join(blocks)
        if seeded:
            text += "\n⚠️ data seed broker (bukan live) — feed MT4 belum pulih"
        return text
    return report.fmt_help(settings.symbol)


def seed_view(hist: dict, price: float) -> tuple[dict, float]:
    """Snapshot dari seed historis — dipakai saat feed live belum tersedia."""
    price_ref = price
    if not price_ref:
        for tf in ("M1", "M5", "M15"):
            if hist.get(tf):
                price_ref = hist[tf][-1].c
                break
    snaps = {tf: snapshot(tf, bars, price_ref or 0.0)
             for tf, bars in hist.items() if bars}
    return snaps, price_ref or 0.0


def announce_text(settings: Settings, *, hist_len: int,
                  live: bool, order_state: str) -> str:
    return (f"✅ <b>Monitor {html.escape(settings.symbol)} aktif</b>\n"
            f"Feed: MT4 EA bridge (satu-satunya sumber tick) · "
            f"seed {hist_len} TF ({settings.history_source})\n"
            f"Mode: {' + '.join(settings.modes)} · "
            f"{'LIVE Telegram' if live else 'DRY-RUN console'}\n"
            f"Order: {html.escape(order_state)}\n"
            f"Command: /status · /planning")


def readiness_payload(*, engines: dict, snaps: dict, hist: dict, price: float,
                      forming, digits: int = 2) -> dict:
    """Kesiapan gate + harga trigger per mode (fallback ke snapshot seed)."""
    if not engines:
        return {}
    if not snaps:
        snaps, price = seed_view(hist, price)
    if not snaps or not price:
        return {}
    now = time.time()
    out: dict = {}
    for mode, eng in engines.items():
        diag = eng.diagnose(snaps, price, now=now, forming_m15=forming)
        if diag.get("missing"):
            out[mode] = {"mode": mode, "percent": 0.0, "remaining_pct": 100.0,
                         "gates_passed": 0, "gates_total": len(readiness.REQUIRED),
                         "next_gate": None, "next_label": "data candle belum siap",
                         "side": None, "trigger_price": None, "distance_usd": None,
                         "distance_pct": None, "plan": None,
                         "note": "; ".join(diag["missing"])}
            continue
        out[mode] = readiness.of_diag(mode, diag, price, digits=digits)
    return out
