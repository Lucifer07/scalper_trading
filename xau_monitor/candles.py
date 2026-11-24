"""Candle building + resampling + structural features (pure, no side effects).

Everything here is deterministic given inputs — the whole strategy layer is
built on top of these primitives, so they must be boring and exact.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

MINUTE = 60
WEEK = 7 * 86_400


@dataclass
class Candle:
    ts: int          # epoch seconds, candle OPEN time (UTC-aligned buckets)
    o: float
    h: float
    l: float
    c: float
    n: int = 0       # ticks/constituents
    complete: bool = False


def week_floor(ts: int) -> int:
    """Monday-aligned week bucket (FX convention)."""
    return ts - ((ts + 3 * 86_400) % WEEK)


def bucket_ts(ts: int, size: int, weekly: bool = False) -> int:
    return week_floor(ts) if weekly else ts - ts % size


class M1Builder:
    """Tick stream -> M1 candles. add_tick returns the just-closed candle."""

    def __init__(self, maxlen: int = 60 * 24 * 14) -> None:
        self.bars: deque[Candle] = deque(maxlen=maxlen)
        self._cur: Candle | None = None

    def add_tick(self, ts_ms: int, price: float) -> Candle | None:
        ts = bucket_ts(ts_ms // 1000, MINUTE)
        closed = None
        if self._cur is None:
            self._cur = Candle(ts, price, price, price, price, 1)
        elif self._cur.ts != ts:
            self._cur.complete = True
            closed = self._cur
            self.bars.append(self._cur)
            self._cur = Candle(ts, price, price, price, price, 1)
        else:
            cur = self._cur
            cur.h = max(cur.h, price)
            cur.l = min(cur.l, price)
            cur.c = price
            cur.n += 1
        return closed

    def closed(self) -> list[Candle]:
        return list(self.bars)


def aggregate(m1: list[Candle], size: int, weekly: bool = False) -> list[Candle]:
    """Fold M1 candles into a higher timeframe. O(n) single pass."""
    out: list[Candle] = []
    for b in m1:
        ts = bucket_ts(b.ts, size, weekly)
        if out and out[-1].ts == ts:
            g = out[-1]
            g.h = max(g.h, b.h)
            g.l = min(g.l, b.l)
            g.c = b.c
            g.n += b.n
        else:
            out.append(Candle(ts, b.o, b.h, b.l, b.c, b.n))
    return out


def mix(history: list[Candle], live: list[Candle]) -> list[Candle]:
    """Seed history (Yahoo) + live bars; live wins on overlapping open-ts."""
    if not live:
        return list(history)
    if not history:
        return list(live)
    cut = live[0].ts
    merged = [b for b in history if b.ts < cut] + list(live)
    return merged


def atr(bars: list[Candle], n: int = 14) -> float:
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(-n, 0):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)))
    return sum(trs) / len(trs)


def pivots(bars: list[Candle], k: int = 2) -> tuple[list[float], list[float]]:
    """Fractal swing highs/lows (confirmed by k bars each side). O(n*k)."""
    hi: list[float] = []
    lo: list[float] = []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i].h == max(b.h for b in win):
            hi.append(bars[i].h)
        if bars[i].l == min(b.l for b in win):
            lo.append(bars[i].l)
    return hi, lo


def cluster(levels: list[float], tol: float) -> list[float]:
    """Merge levels within `tol` into one representative (the mean)."""
    if not levels:
        return []
    out: list[list[float]] = [[levels[0]]]
    for x in sorted(levels)[1:]:
        if x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(g) / len(g) for g in out]


@dataclass
class TFSnap:
    tf: str
    bars: list[Candle]
    price: float
    atr: float
    pos20: float                      # % position within last-20-bar range
    res_above: list[float]            # clustered swing resistances above price
    sup_below: list[float]            # clustered swing supports below price
    last_wick: tuple[float, float]    # (upper%, lower%) wick ratio of last bar
    chop_flips: int                   # direction changes across last 12 bars
    trend: str = "FLAT"


def snapshot(tf: str, bars: list[Candle], price: float, k: int = 2) -> TFSnap:
    lb = bars[-20:]
    rng_hi = max((b.h for b in lb), default=price)
    rng_lo = min((b.l for b in lb), default=price)
    pos = (price - rng_lo) / (rng_hi - rng_lo) * 100 if rng_hi > rng_lo else 50.0

    a = atr(bars) or 1e-9
    hi, lo = pivots(bars, k)
    res = cluster([x for x in hi if x > price], 0.25 * a)
    sup = cluster([x for x in lo if x < price], 0.25 * a)
    sup.sort(reverse=True)

    last = bars[-1]
    rng = (last.h - last.l) or 1e-9
    body_hi, body_lo = max(last.o, last.c), min(last.o, last.c)
    wick = ((last.h - body_hi) / rng, (body_lo - last.l) / rng)

    ch = bars[-12:]
    flips = 0
    for i in range(2, len(ch)):
        d1 = ch[i - 1].c - ch[i - 2].c
        d2 = ch[i].c - ch[i - 1].c
        if d1 * d2 < 0:
            flips += 1

    return TFSnap(tf, bars, price, a, pos, res, sup, wick, flips)
