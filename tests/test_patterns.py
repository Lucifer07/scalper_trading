"""Pattern detector tests — pure functions, exact synthetic candles."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.candles import Candle  # noqa: E402
from xau_monitor import patterns as pat  # noqa: E402

BASE = 1_699_999_800


def c(o, h, l, cl, i=0):
    return Candle(BASE + i * 300, o, h, l, cl)


def test_wick_ratio():
    up, dn = pat.wick(c(10, 14, 8, 12))   # body 10-12, wick atas 2, bawah 2 dari 6
    assert abs(up - 2 / 6) < 1e-6 and abs(dn - 2 / 6) < 1e-6


def test_jarum_single():
    assert pat.jarum_single(c(10.5, 15, 9.8, 10.2), "SELL")   # atas 86%, bearish
    assert not pat.jarum_single(c(10.5, 15, 9.8, 10.2), "BUY")
    assert not pat.jarum_single(c(10, 10.6, 9.8, 10.4), "SELL")  # wick kecil


def test_jarum_series_pola_2():
    bars = [c(10, 10.8, 9.9, 10.2), c(10.2, 10.9, 10.0, 10.1), c(10.1, 10.5, 9.9, 10.0)]
    assert pat.jarum_series(bars, "SELL")          # 2 dari 3 berjarum atas + close bearish
    assert not pat.jarum_series(bars[:1], "SELL")  # data kurang


def test_jebol_jarum_returns_ref_level():
    jarum_bar = c(10.0, 11.0, 9.9, 10.4)     # jarum atas tinggi 11.0
    pierce = c(10.4, 11.3, 10.3, 10.9)       # tembus 11.3, close balik < 11.0
    back = c(10.9, 11.0, 10.5, 10.7)
    assert pat.jebol_jarum([jarum_bar, pierce, back], "SELL") == 11.0
    pierce_close_above = c(10.4, 11.3, 10.3, 11.2)  # tembus tapi close di atas jarum
    assert pat.jebol_jarum([jarum_bar, pierce_close_above], "SELL") is None


def test_kocokan_at_zone():
    zone = 100.0
    alt = [c(99.6, 100.6, 99.4, 100.4), c(100.4, 100.5, 99.5, 99.8),
           c(99.8, 100.7, 99.7, 100.5), c(100.5, 100.6, 99.6, 99.9)]
    assert pat.kocokan_at_zone(alt, zone, tol=0.3)
    trend = [c(100, 101, 99.9, 100.9), c(100.9, 101.9, 100.8, 101.8)]
    assert not pat.kocokan_at_zone(trend, zone, tol=0.3)


def test_thrust_last():
    small = [c(10, 10.2, 9.9, 10.1)] * 3
    big = c(10.1, 12.0, 10.0, 11.8)          # range 2.0 >= 1.2*1.0
    bars = small + [big, c(11.8, 11.9, 11.5, 11.6)]
    assert pat.thrust_last(bars, "SELL", atr=1.0)
    assert not pat.thrust_last(bars, "BUY", atr=1.0)  # salah arah


def test_sweep_both_sides():
    zone, atr = 100.0, 2.0
    bars = [c(100, 101.0, 99.0, 100.2), c(100.2, 101.5, 98.8, 100.1)]  # >101 & <99
    assert pat.sweep_both_sides(bars, zone, atr)
    one_side = [c(100, 101.0, 99.5, 100.2)]
    assert not pat.sweep_both_sides(one_side, zone, atr)


def test_last_minutes_full():
    forming = c(3890.0, 3892.2, 3889.8, 3891.8)  # bullish body 1.8/2.4 = 75%
    bucket = BASE - (BASE % 900)                 # bucket M15 yang sejajar
    assert pat.last_minutes_full(forming, "SELL", now=bucket + 800)
    assert not pat.last_minutes_full(forming, "SELL", now=bucket + 400)
    assert not pat.last_minutes_full(None, "SELL", now=bucket + 800)
