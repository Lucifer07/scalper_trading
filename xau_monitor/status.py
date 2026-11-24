"""Bentuk data status monitor (JSON) yang dikonsumsi control API / panel GUI.

Fungsi murni: pemanggil menyiapkan angka, modul ini hanya memformat kontrak
JSON agar panel tidak perlu tahu objek internal runner.
"""
from __future__ import annotations


def payload(*, symbol: str, price: float, uptime_s: float, tick: dict,
            engines: dict, orders: dict, broker: dict,
            dry_run: bool, readiness: dict | None = None,
            digits: int = 2) -> dict:
    return {
        "symbol": symbol,
        "price": round(price, digits) if price else 0.0,
        "digits": max(0, int(digits)),
        "uptime_s": round(uptime_s, 1),
        "dry_run": dry_run,
        "tick": tick,
        "engines": engines,
        "orders": orders,
        "broker": broker,
        "readiness": readiness or {},
    }


def engines_payload(engines: dict) -> dict:
    return {
        mode: {"state": eng.state.value, "blocked": eng.blocked_reason or ""}
        for mode, eng in engines.items()
    }


def tick_payload(*, rate: float, mt4_ticks: int, mt4_age_s: float) -> dict:
    """Tick selalu dari MT4 — tidak ada lagi sumber kantor/FIX worker."""
    return {
        "source": "mt4",
        "rate": round(rate, 1),
        "mt4_ticks": mt4_ticks,
        "mt4_age_s": None if mt4_age_s >= 1e9 else round(mt4_age_s, 1),
    }
