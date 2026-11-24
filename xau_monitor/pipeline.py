"""Primitif pipeline tick → candle → snapshot (dipindah dari `main`).

Fungsi murni/IO-ringan yang dipakai runner; dipisah agar `main` tetap tipis
dan helper ini bisa diuji tanpa menyalakan feed.
"""
from __future__ import annotations

import html
import logging
import queue
import time

from .candles import WEEK, Candle, M1Builder, aggregate, bucket_ts, mix, snapshot

log = logging.getLogger("pipeline")

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H4": 14400, "D1": 86400}
FIRST_TICK_TIMEOUT_S = 90


def complete_only(bars: list[Candle], size: int, weekly: bool, now: float) -> list[Candle]:
    """Buang bucket yang masih berjalan — sinyal tidak boleh baca candle parsial."""
    if not bars:
        return bars
    cur = bucket_ts(int(now), size, weekly)
    return bars[:-1] if bars[-1].ts == cur else bars


def deliver(notifier, text: str) -> None:
    if notifier is None:
        print("\n" + "=" * 62 + "\n" + text + "\n" + "=" * 62, flush=True)
        return
    if not notifier.send(text):
        log.error("GAGAL KIRIM TELEGRAM: %s", text[:120])


def build_snaps(hist: dict, builder: M1Builder, price: float) -> dict:
    now = time.time()
    m1 = builder.closed()
    snaps: dict = {}
    for tf, size in TF_SECONDS.items():
        bars = mix(hist.get(tf, []), aggregate(m1, size))
        bars = complete_only(bars, size, False, now)
        if bars:
            snaps[tf] = snapshot(tf, bars, price)
    bars = mix(hist.get("W1", []), aggregate(m1, WEEK, weekly=True))
    bars = complete_only(bars, WEEK, True, now)
    if bars:
        snaps["W1"] = snapshot("W1", bars, price)
    return snaps


def forming_bar(m1: list[Candle], price: float, now: float, minutes: int = 15) -> Candle:
    """Candle TF yang masih berjalan (untuk aturan 'menit terakhir')."""
    size = minutes * 60
    bucket = int(now) - int(now) % size
    seg = [b for b in m1 if b.ts >= bucket]
    o = seg[0].o if seg else price
    h = max([b.h for b in seg] + [price])
    l = min([b.l for b in seg] + [price])
    return Candle(bucket, o, h, l, price, len(seg))


def market_dead(bars: list[Candle], price: float, flat_bars: int) -> bool:
    """True bila feed dianggap beku: N bar M1 terakhir ber-OHLC identik
    sempurna dan harga live persis sama dengan close terakhir.

    Deteksi berbasis data, bukan jam market. Saat market tutup, EA MT4
    terus mengembalikan bar terakhir yang sebenarnya (high/low TIDAK datar),
    namun offset gmt-nya menyusut 1 detik/menit sehingga builder menutup
    "bar baru" tiap menit dengan OHLC yang sama persis. Evaluasi di atas
    data beku hanya menghasilkan HOLD palsu (window 20-bar M15 bergeser
    sendiri) dan risiko order saat market tutup. Harga bergerak satu tick
    saja -> guard lepas (release instan, tidak menunggu bar close).
    """
    if flat_bars < 2:
        # ponytail: dipaksa minimal 2 oleh config; jaga di sini juga agar
        # satu bar doji tunggal tidak pernah dianggap market tutup.
        return False
    if len(bars) < flat_bars:
        return False
    if price != bars[-1].c:
        return False
    ref = bars[-1]
    return all(b.o == ref.o and b.h == ref.h and b.l == ref.l and b.c == ref.c
               for b in bars[-flat_bars:])


def wait_first_tick(outq: "queue.Queue[tuple[str, int, float]]",
                    timeout: float = FIRST_TICK_TIMEOUT_S) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, _, mid = outq.get(timeout=2.0)
            return mid
        except queue.Empty:
            continue
    return None


def atr_m1(snaps: dict) -> float:
    for tf in ("M1", "M5"):
        snap = snaps.get(tf)
        if snap is not None and snap.atr > 0:
            return snap.atr
    return 0.0


def escape_text(text: str) -> str:
    """Alasan/log yang masuk pesan Telegram HTML harus di-escape."""
    return html.escape(text, quote=False)
