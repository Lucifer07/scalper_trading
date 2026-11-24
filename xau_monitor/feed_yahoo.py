"""Yahoo context seeding — TF besar (W1/D1/H4/H1/M30/M15/M5/M1) history.

The office feed only streams live ticks, so higher-TF structure is seeded
from Yahoo (GC=F proxy) and shifted by a basis so it lines up with the
office spot mid. Best effort: missing TFs are skipped loudly, never faked.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

from .candles import Candle, aggregate

log = logging.getLogger("yahoo")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
# interval -> (TF name, yahoo interval, range)
SPECS = [
    ("W1", "1wk", "3y"),
    ("D1", "1d", "1y"),
    ("H1", "60m", "30d"),
    ("M30", "30m", "30d"),
    ("M15", "15m", "20d"),
    ("M5", "5m", "10d"),
    ("M1", "1m", "4d"),
]


def _fetch(symbol: str, interval: str, rng: str) -> list[Candle]:
    last_err: Exception | None = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval={interval}&range={rng}")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            bars = []
            for i, t in enumerate(ts):
                if q["close"][i] is None:
                    continue
                bars.append(Candle(t, q["open"][i], q["high"][i],
                                   q["low"][i], q["close"][i]))
            if bars:
                return bars
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(f"yahoo {symbol} {interval}: {last_err}")


def seed(symbol: str, live_mid: float | None = None) -> dict[str, list[Candle]]:
    """Fetch all context TFs; shift prices by (live_mid - latest close)."""
    out: dict[str, list[Candle]] = {}
    basis = 0.0
    for tf, itv, rng in SPECS:
        try:
            bars = _fetch(symbol, itv, rng)
        except Exception as e:
            log.warning("seed %s gagal (dilewati): %s", tf, e)
            continue
        if live_mid is not None and bars:
            basis = live_mid - bars[-1].c
            for b in bars:
                b.o += basis
                b.h += basis
                b.l += basis
                b.c += basis
        out[tf] = bars
        log.info("seed %s: %d bar (basis %+.2f)", tf, len(bars), basis)

    if "H1" in out:
        out["H4"] = aggregate(out["H1"], 4 * 3600)
        log.info("seed H4: %d bar (resample H1)", len(out["H4"]))
    return out
