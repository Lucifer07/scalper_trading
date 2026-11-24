"""Unit tests for candle primitives — must be exact, the strategy relies on them."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.candles import (  # noqa: E402
    Candle, M1Builder, aggregate, cluster, mix, pivots,
)

MIN = 60


def tick(ts_ms, price):
    return ts_ms, price


def test_m1_builder_same_minute_no_close():
    b = M1Builder()
    t0 = 1_700_000_000_000  # inside one minute
    assert b.add_tick(t0, 100.0) is None
    assert b.add_tick(t0 + 5_000, 101.0) is None
    cur = b._cur
    assert cur.o == 100.0 and cur.c == 101.0 and cur.n == 2


def test_m1_builder_rollover_closes_candle():
    b = M1Builder()
    t0 = 1_700_000_000_000
    b.add_tick(t0, 100.0)
    b.add_tick(t0 + 10_000, 102.0)
    b.add_tick(t0 + 20_000, 99.0)
    closed = b.add_tick(t0 + MIN * 1000, 98.0)  # next minute
    assert closed is not None
    assert closed.complete
    assert (closed.o, closed.h, closed.l, closed.c) == (100.0, 102.0, 99.0, 99.0)
    assert closed.n == 3
    assert b._cur.o == 98.0


def _bars(closes, base_ts=1_699_999_800):  # aligned ke bucket 5-menit
    out, prev = [], None
    for i, c in enumerate(closes):
        o = prev if prev is not None else c
        out.append(Candle(base_ts + i * MIN, o, max(o, c) + 1, min(o, c) - 1, c))
        prev = c
    return out


def test_aggregate_m5_ohl():
    m1 = _bars([10, 12, 11, 13, 12, 14])  # two M5 candles worth (5m + 1m)
    m5 = aggregate(m1, 5 * MIN)
    assert len(m5) == 2
    assert m5[0].o == 10 and m5[0].c == 12
    assert m5[0].h == 14 and m5[0].l == 9  # +1/-1 envelope in helper
    assert m5[1].c == 14


def test_pivots_find_swing_high_low():
    closes = [10, 11, 12, 11, 10, 9, 10, 11, 10]
    bars = _bars(closes)
    hi, lo = pivots(bars, k=2)
    assert max(hi) > 12  # swing high around index 2 (12+1 envelope)
    assert min(lo) < 9


def test_cluster_merges_close_levels():
    assert cluster([100.0, 100.4, 101.5], tol=0.5) == [100.2, 101.5]
    assert cluster([], 1.0) == []
    assert cluster([50.0], 1.0) == [50.0]


def test_mix_live_overlaps_history():
    hist = _bars([1, 2, 3, 4], base_ts=0)
    live = _bars([4, 5], base_ts=3 * MIN)  # overlaps ts of hist[3]
    merged = mix(hist, live)
    assert len(merged) == 5  # hist[0..2] + live(2) — hist yang overlap dibuang
    assert merged[-1].c == 5
    assert merged[-2].c == 4
    assert merged[-3].c == 3
