"""Pure candle-pattern detectors from the .md framework (stateless, testable)."""
from __future__ import annotations

from .candles import Candle


def wick(b: Candle) -> tuple[float, float]:
    """(upper%, lower%) wick share of the candle range."""
    rng = (b.h - b.l) or 1e-9
    top, bot = max(b.o, b.c), min(b.o, b.c)
    return (b.h - top) / rng, (bot - b.l) / rng


def jarum_single(b: Candle, bias: str, wick_min: float = 0.55) -> bool:
    """Pola 'jarum-jarum': wick >=55% melawan arah + close searah bias."""
    up, dn = wick(b)
    if bias == "SELL":
        return up >= wick_min and b.c < b.o
    return dn >= wick_min and b.c > b.o


def jarum_series(bars: list[Candle], bias: str, lookback: int = 3,
                 need: int = 2, wick_min: float = 0.45) -> bool:
    """Pola no.2 (.md): >=2 dari 3 candle berurutan berjarum, close terakhir searah."""
    seg = bars[-lookback:]
    if len(seg) < need:
        return False
    hits = 0
    for b in seg:
        up, dn = wick(b)
        if (bias == "SELL" and up >= wick_min) or (bias == "BUY" and dn >= wick_min):
            hits += 1
    last = seg[-1]
    polarity = last.c < last.o if bias == "SELL" else last.c > last.o
    return hits >= need and polarity


def jebol_jarum(bars: list[Candle], bias: str, lookback: int = 6,
                wick_min: float = 0.5) -> float | None:
    """Entry setelah jarum terjebol (.md): wick ditembus lalu gagal, close balik.

    Returns level referensi stop (high jarum untuk SELL / low jarum untuk BUY).
    """
    seg = bars[-lookback:]
    for i in range(len(seg) - 2, -1, -1):
        b = seg[i]
        up, dn = wick(b)
        if bias == "SELL" and up >= wick_min:
            pierced = max((x.h for x in seg[i + 1:]), default=-1e9)
            if pierced > b.h and seg[-1].c < b.h:
                return b.h
        if bias == "BUY" and dn >= wick_min:
            pierced = min((x.l for x in seg[i + 1:]), default=1e9)
            if pierced < b.l and seg[-1].c > b.l:
                return b.l
    return None


def kocokan_at_zone(bars: list[Candle], zone: float, tol: float,
                    lookback: int = 6, min_touch: int = 2,
                    min_flips: int = 2) -> bool:
    """Harga 'bermain-main di pucuk' (.md §6/§8): bolak-balik menyentuh zona."""
    seg = [b for b in bars[-lookback:] if b.l <= zone + tol and b.h >= zone - tol]
    if len(seg) < min_touch:
        return False
    flips = 0
    for i in range(1, len(seg)):
        d1 = seg[i - 1].c - seg[i - 1].o
        d2 = seg[i].c - seg[i].o
        if d1 * d2 < 0:
            flips += 1
    return flips >= min_flips


def thrust_last(bars: list[Candle], bias: str, atr: float,
                lookback: int = 6, mult: float = 1.2) -> bool:
    """Dorongan terakhir sebelum reversal: candle besar melawan bias di segmen akhir."""
    seg = bars[-lookback:-1] if len(bars) > lookback else bars[:-1]
    for b in seg:
        rng = b.h - b.l
        if bias == "SELL" and b.c > b.o and rng >= mult * atr:
            return True
        if bias == "BUY" and b.c < b.o and rng >= mult * atr:
            return True
    return False


def sweep_both_sides(bars: list[Candle], zone: float, atr: float,
                     lookback: int = 6, mult: float = 0.5) -> bool:
    """Jebol ganda (.md §7): dua sisi zona tersapu → buy & sell dua-duanya kena."""
    seg = bars[-lookback:]
    up = any(b.h > zone + mult * atr for b in seg)
    dn = any(b.l < zone - mult * atr for b in seg)
    return up and dn


def last_minutes_full(forming: Candle | None, bias: str, now: float | None,
                      bucket: int = 900, window: float = 120.0) -> bool:
    """Menit terakhir M15 masih full melawan bias → jangan lawan (.md §8)."""
    if forming is None or now is None:
        return False
    if bucket - (now % bucket) > window:
        return False
    rng = forming.h - forming.l
    if rng <= 0:
        return False
    if abs(forming.c - forming.o) < 0.5 * rng:
        return False
    return (bias == "SELL" and forming.c > forming.o) or \
           (bias == "BUY" and forming.c < forming.o)
