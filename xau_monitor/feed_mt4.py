"""Seed struktur TF besar langsung dari MT4 (bukan Yahoo).

Alasan: bar broker adalah sumber yang sama dengan tempat order dieksekusi —
tidak ada basis futures (`GC=F`), tidak ada data tertinggal sejam. Feed kantor
tetap penyedia tick realtime; modul ini hanya mengisi H1/H4/D1/W1 (+M15/M30)
saat start dan saat refresh.

Normalisasi: EA mengirim bar tertutup dengan **server time** dan offset GMT;
di sini waktu dikonversi ke UTC lalu di-bucket ulang dengan konvensi internal
(`bucket_ts`/`week_floor`) supaya tidak dobel dengan bar hasil agregasi live.
"""
from __future__ import annotations

import logging
import time

from .candles import WEEK, Candle, bucket_ts
from .mt4_link import Mt4Error, Mt4Link, Mt4Rejected, Mt4Unavailable
from .mt4_wire import as_int

log = logging.getLogger("mt4seed")

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400,
              "D1": 86400, "W1": WEEK}
SEED_COUNTS = {"M1": 480, "M5": 300, "M15": 300, "M30": 300, "H1": 300, "H4": 250,
               "D1": 250, "W1": 200}


class SeedError(RuntimeError):
    """History broker tidak bisa diperoleh — monitor tidak boleh jalan buta."""


def seed(link: Mt4Link, symbol: str, counts: dict[str, int] | None = None,
         retry_seconds: float = 45.0) -> dict[str, list[Candle]]:
    counts = counts or SEED_COUNTS
    out: dict[str, list[Candle]] = {}
    for tf, count in counts.items():
        out[tf] = fetch(link, symbol, tf, count, retry_seconds)
        log.info("seed %s %s: %d bar", symbol, tf, len(out[tf]))
    return out


def fetch(link: Mt4Link, symbol: str, tf: str, count: int,
          retry_seconds: float) -> list[Candle]:
    if tf not in TF_SECONDS:
        raise SeedError(f"TF tidak didukung seed: {tf}")
    deadline = time.time() + retry_seconds
    while True:
        try:
            resp = link.call("rates", sym=symbol, tf=tf, count=count, timeout=15.0)
            return parse_bars(resp, tf)
        except Mt4Rejected as exc:
            if exc.code != "no_history":
                raise SeedError(f"{symbol} {tf} ditolak broker: {exc.code}") from exc
            reason = "broker belum mengirim history"
        except Mt4Unavailable as exc:
            reason = f"EA belum terhubung ({exc})"
        except Mt4Error as exc:
            raise SeedError(f"bridge MT4 ({symbol} {tf}): {exc}") from exc
        if time.time() >= deadline:
            raise SeedError(
                f"{symbol} {tf}: {reason} setelah {retry_seconds:.0f}s "
                f"(pastikan EA XauBridge menempel di chart & MT4 login)")
        log.info("%s %s: %s — coba lagi", symbol, tf, reason)
        time.sleep(2.0)


def parse_bars(resp: dict[str, str], tf: str) -> list[Candle]:
    offset = as_int(resp, "gmt", 0)
    size, weekly = TF_SECONDS[tf], tf == "W1"
    raw = [rec for rec in resp.get("bars", "").split("~") if rec]
    if not raw:
        raise SeedError(f"{tf}: respons rates kosong")
    by_ts: dict[int, Candle] = {}
    for rec in raw:
        parts = rec.split(",")
        if len(parts) != 6:
            raise SeedError(f"{tf}: record bar rusak: {rec[:60]!r}")
        try:
            ts = int(parts[0]) - offset
            bar = Candle(bucket_ts(ts, size, weekly), float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), int(float(parts[5])))
        except ValueError as exc:
            raise SeedError(f"{tf}: angka bar tidak valid: {rec[:60]!r}") from exc
        if bar.h < bar.l or bar.c <= 0:
            raise SeedError(f"{tf}: bar tidak wajar: {rec[:60]!r}")
        by_ts[bar.ts] = bar
    return [by_ts[ts] for ts in sorted(by_ts)]
