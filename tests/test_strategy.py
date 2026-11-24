"""Strategy tests — realistic 3890-scale fixtures + all gates/modes."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.candles import Candle, TFSnap  # noqa: E402
from xau_monitor.strategy import Engine, trend  # noqa: E402

BASE = 1_699_999_800
STEP = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
        "H4": 14400, "D1": 86400, "W1": 604800}


def mk(points, tf):
    return [Candle(BASE + i * STEP[tf], o, h, l, c)
            for i, (o, h, l, c) in enumerate(points)]


def snap(tf, points, price, atr, pos20, res=(), sup=()):
    return TFSnap(tf, mk(points, tf), price, atr, pos20,
                  list(res), list(sup), (0.1, 0.1), 0)


def desc(n=8, start=4000.0, step=10.0, pad=2.0, up=False):
    pts, prev = [], None
    for i in range(n):
        c = start + i * step if up else start - i * step
        o = prev if prev is not None else (start - step * 0.5 if up else start + step * 0.5)
        pts.append((o, max(o, c) + pad, min(o, c) - pad, c))
        prev = c
    return pts


M15_PTS = [(3897.0, 3897.4, 3896.6, 3896.8), (3896.7, 3897.0, 3895.9, 3896.1),
           (3896.0, 3896.2, 3895.2, 3895.4), (3895.3, 3895.6, 3894.6, 3894.8),
           (3894.7, 3895.0, 3894.0, 3894.2), (3894.2, 3894.6, 3893.6, 3893.8),
           (3893.7, 3894.0, 3893.0, 3893.2), (3893.1, 3893.9, 3892.8, 3893.5)]

M5_PTS = [(3889.0, 3892.0, 3888.8, 3891.8), (3891.8, 3892.1, 3889.4, 3889.6),
          (3889.6, 3892.5, 3889.5, 3892.3), (3892.3, 3892.4, 3890.2, 3890.4),
          (3890.4, 3893.0, 3890.3, 3892.8), (3891.5, 3893.8, 3891.3, 3891.4)]


def fixture(h4_up=False, m5_override=None, m15_override=None):
    h4_pts = desc(8, 3990, 15, 3, up=h4_up)
    m5 = m5_override or M5_PTS
    m15 = m15_override or M15_PTS
    return {
        "W1": snap("W1", desc(8, 4600, 40, 6), 3891.0, 200.0, 5.0),
        "D1": snap("D1", desc(8, 4000, 10, 2), 3891.0, 98.0, 8.0),
        "H4": snap("H4", h4_pts, 3891.0, 40.0, 10.0 if not h4_up else 90.0),
        "H1": snap("H1", desc(8, 3960, 12, 2), 3891.0, 30.0, 12.0,
                   res=[3927.0], sup=[3871.0]),
        "M30": snap("M30", [(3892, 3893, 3891, 3892)] * 8, 3891.0, 6.0, 45.0),
        # Default 85% = M15 "naik tinggi": memenuhi gate entry sniper SELL
        # (arah D1/H4/H1 DOWN). Test handgun/BUY menimpanya secara eksplisit.
        "M15": snap("M15", m15, 3891.0, 5.0, 85.0,
                    res=[3891.5], sup=[3881.0, 3877.0]),
        "M5": snap("M5", m5, 3891.0, 2.0, 50.0),
        "M1": snap("M1", [(3891, 3891.2, 3890.8, 3891)] * 8, 3891.0, 1.5, 50.0),
    }


def test_trend_classification():
    assert trend(snap("W1", desc(8, 4000, 10, 2), 3850.0, 98.0, 5.0)) == "DOWN"
    assert trend(snap("W1", desc(8, 3800, 10, 2, up=True), 3920.0, 98.0, 90.0)) == "UP"
    flat = snap("W1", [(10, 10, 10, 10)] * 8, 10.0, 5.0, 50.0)
    assert trend(flat) == "FLAT"


def test_sniper_all_gates_emit_sell():
    e = Engine()
    sig = e.step(fixture(), 3891.0, now=1_000.0)
    assert sig is not None and sig.kind == "ENTRY_SELL"
    assert sig.zone == 3891.5
    assert 3893.8 < sig.stop < 3895.5            # di atas jarum 3893.8 + buffer
    assert sig.tps == [3881.0, 3877.0, 3871.0]
    assert sig.invalid_level > 3927.0
    assert e.state.value == "COOLDOWN"


def test_gate1_misaligned_blocks():
    e = Engine()
    assert e.step(fixture(h4_up=True), 3891.0, now=1_000.0) is None
    assert "seirama" in e.blocked_reason


def test_gate2_not_extreme_blocks():
    # Sniper: arah DOWN -> entry SELL wajib M15 "naik tinggi" (>=80%).
    e = Engine()
    snaps = fixture()
    snaps["M15"].pos20 = 50.0  # belum naik tinggi
    assert e.step(snaps, 3891.0, now=1_000.0) is None
    assert "naik tinggi" in e.blocked_reason


def test_kocokan_missing_blocks():
    m5 = [(3900.0, 3900.4, 3899.8, 3900.2)] * 6  # jauh dari zona, tanpa kocokan
    e = Engine()
    assert e.evaluate(fixture(m5_override=m5), 3891.0) is None
    assert "kocokan" in e.blocked_reason


def test_thrust_missing_blocks():
    small = [(3891.2, 3891.6, 3891.0, 3891.4), (3891.4, 3891.8, 3891.2, 3891.3),
             (3891.3, 3891.7, 3891.1, 3891.5), (3891.5, 3891.9, 3891.3, 3891.4),
             (3891.4, 3891.8, 3891.2, 3891.6), (3891.6, 3891.9, 3891.4, 3891.5)]
    e = Engine()
    assert e.evaluate(fixture(m5_override=small), 3891.0) is None
    assert "dorongan" in e.blocked_reason


def test_sweep_both_sides_veto():
    m15 = list(M15_PTS)
    m15[3] = (3895.3, 3895.6, 3885.0, 3894.8)  # low menembus zone-0.5*ATR
    e = Engine()
    assert e.evaluate(fixture(m15_override=m15), 3891.0) is None
    assert "jebol ganda" in e.blocked_reason


def test_veto_big_green_wick_candle():
    m15 = list(M15_PTS)
    # range 7.2, body 4.0 (56%), jarum atas 3.2 (45%) -> veto
    m15[-1] = (3889.0, 3896.2, 3889.0, 3893.0)
    e = Engine()
    assert e.evaluate(fixture(m15_override=m15), 3891.0) is None
    assert "veto pola 1" in e.blocked_reason


def test_last_minutes_full_blocks():
    e = Engine()
    bucket = BASE - (BASE % 900)  # bucket M15 yang benar-benar sejajar
    forming = Candle(bucket, 3890.0, 3892.2, 3889.8, 3891.8)  # bullish kuat
    assert e.evaluate(fixture(), 3891.0, now=bucket + 800,
                      forming_m15=forming) is None
    assert "menit terakhir" in e.blocked_reason


def test_handgun_mode_skips_daily_gate():
    e = Engine(mode="handgun")
    fx = fixture(h4_up=True)          # D1/H4/H1 tidak seirama
    fx["M15"].pos20 = 90.0            # M15 naik tinggi -> SELL (bukan tren M15)
    sig = e.step(fx, 3891.0, now=1_000.0)
    assert sig is not None and sig.kind == "ENTRY_SELL"
    assert sig.tps == [3881.0, 3877.0]  # zona & TP hanya M15


def test_handgun_needs_m15_extreme():
    e = Engine(mode="handgun")
    fx = fixture()
    fx["M15"].pos20 = 50.0            # netral — bukan naik tinggi / turun dalam
    assert e.evaluate(fx, 3891.0) is None
    assert "belum ekstrem" in e.blocked_reason


def test_sniper_entry_waits_for_m15_pullback():
    # Video 21:06 / 14:36: arah D1/H4/H1 DOWN -> SELL hanya setelah M15 naik tinggi.
    e = Engine()
    fx = fixture()
    fx["M15"].pos20 = 30.0          # belum naik tinggi
    assert e.evaluate(fx, 3891.0) is None
    assert "naik tinggi" in e.blocked_reason

    e2 = Engine()
    fx2 = fixture()
    fx2["M15"].pos20 = 85.0         # naik tinggi -> SELL
    sig = e2.evaluate(fx2, 3891.0)
    assert sig is not None and sig.kind == "ENTRY_SELL"


def test_sniper_buy_requires_m15_turun_dalam():
    # Arah besar UP. BUY hanya boleh setelah M15 turun dalam (pullback), bukan di puncak.
    m5_buy = [(3892.0, 3892.2, 3889.0, 3889.2),
              (3889.2, 3890.8, 3888.8, 3890.6),
              (3890.6, 3891.6, 3890.2, 3891.4),
              (3891.4, 3891.8, 3890.4, 3890.6),
              (3890.6, 3890.9, 3890.3, 3890.7),
              (3890.7, 3892.0, 3890.5, 3891.8)]
    fx = fixture(m5_override=m5_buy)
    fx["D1"] = snap("D1", desc(8, 3900, 10, 2, up=True), 3891.0, 98.0, 90.0)
    fx["H4"] = snap("H4", desc(8, 3990, 15, 3, up=True), 3891.0, 40.0, 90.0)
    fx["H1"] = snap("H1", desc(8, 3960, 12, 2, up=True), 3891.0, 30.0, 90.0,
                    res=[3927.0], sup=[3871.0])
    fx["M15"] = snap("M15", M15_PTS, 3891.0, 5.0, 85.0,
                     res=[3891.5], sup=[3890.5])
    e = Engine()
    assert e.evaluate(fx, 3891.0) is None      # M15 masih di puncak -> tolak
    assert "turun dalam" in e.blocked_reason

    fx2 = fixture(m5_override=m5_buy)
    fx2["D1"], fx2["H4"], fx2["H1"] = fx["D1"], fx["H4"], fx["H1"]
    fx2["M15"] = snap("M15", M15_PTS, 3891.0, 5.0, 10.0,
                      res=[3891.5], sup=[3890.5])   # turun dalam -> BUY
    e2 = Engine()
    sig = e2.evaluate(fx2, 3891.0)
    assert sig is not None and sig.kind == "ENTRY_BUY"


def test_limit_offset_2pct_of_last_m5_capped_10pips():
    # XAUUSD digits=2 -> pip=0.01, cap 10 pip = 0.10
    e = Engine()
    s = snap("M5", [(3891.0, 3893.0, 3891.0, 3892.0)], 3891.0, 2.0, 50.0)
    assert abs(e._limit_offset(s) - 0.04) < 1e-9      # range 2.0 x 2%
    big = snap("M5", [(3800.0, 4000.0, 3800.0, 3990.0)], 3891.0, 2.0, 50.0)
    assert abs(e._limit_offset(big) - 0.10) < 1e-9    # kena cap 10 pip
    # EURUSD digits=5 -> pip=0.0001, cap 0.0010
    fx = Engine(digits=5)
    s5 = snap("M5", [(1.3000, 1.3020, 1.2990, 1.3010)], 1.30, 0.001, 50.0)
    assert abs(fx._limit_offset(s5) - 0.00006) < 1e-12


def test_stop_stays_on_correct_side_when_structure_is_overtaken():
    # BUY: harga sudah turun melewati low candle M5 terakhir (stop_ref > price).
    # Stop wajib tetap < harga, kalau tidak OrderIntent menolak intent_invalid.
    e = Engine(mode="handgun")
    m5_buy = [(3892.0, 3892.2, 3889.0, 3889.2),
              (3889.2, 3890.8, 3888.8, 3890.6),
              (3890.6, 3891.6, 3890.2, 3891.4),
              (3891.4, 3891.8, 3890.4, 3890.6),
              (3890.6, 3890.9, 3890.3, 3890.7),
              (3890.7, 3892.0, 3890.5, 3891.8)]
    fx = fixture(m5_override=m5_buy)
    fx["M15"].pos20 = 10.0          # turun dalam -> BUY
    fx["M15"].sup_below = [3890.5]
    sig = e.evaluate(fx, 3890.0)   # harga di bawah low M5 (3890.5)
    assert sig is not None and sig.kind == "ENTRY_BUY"
    assert sig.stop < sig.price

    # SELL: harga sudah naik melewati high candle M5 terakhir.
    e2 = Engine(mode="handgun")
    fx2 = fixture()
    fx2["M15"].pos20 = 90.0
    sig2 = e2.evaluate(fx2, 3892.5)  # di atas high M5 terakhir (3893.8? dicek di bawah)
    assert sig2 is not None and sig2.kind == "ENTRY_SELL"
    assert sig2.stop > sig2.price


def test_handgun_direction_follows_extreme_not_trend():
    # Koreksi video: naik tinggi -> JUAL, turun dalam -> BELI.
    # Sebelum perbaikan, arah diambil dari tren M15 (UP->BUY) sehingga engine
    # membeli di puncak range -> 100% ditolak advisor sebagai chasing.
    e = Engine(mode="handgun")
    high = fixture()
    high["M15"].pos20 = 90.0
    assert e.evaluate(high, 3891.0).kind == "ENTRY_SELL"
    assert "naik tinggi" in e.evaluate(high, 3891.0).reasons[0]

    e2 = Engine(mode="handgun")
    m5_buy = [(3892.0, 3892.2, 3889.0, 3889.2),   # thrust turun (dorongan terakhir)
              (3889.2, 3890.8, 3888.8, 3890.6),   # kocokan: naik
              (3890.6, 3891.6, 3890.2, 3891.4),
              (3891.4, 3891.8, 3890.4, 3890.6),   # kocokan: turun
              (3890.6, 3890.9, 3890.3, 3890.7),
              (3890.7, 3892.0, 3890.5, 3891.8)]
    low = fixture(m5_override=m5_buy)
    low["M15"].pos20 = 10.0           # turun dalam -> BUY
    low["M15"].sup_below = [3890.5]   # support dekat harga supaya lolos gate zona
    sig = e2.evaluate(low, 3891.0)
    assert sig is not None and sig.kind == "ENTRY_BUY"
    assert "turun dalam" in sig.reasons[0]


def test_duplicate_suppressed_and_invalidation():
    e = Engine()
    now = 1_000.0
    assert e.step(fixture(), 3891.0, now) is not None
    assert e.step(fixture(), 3891.5, now + 10) is None
    inv = e.step(fixture(), 3940.0, now + 60)
    assert inv is not None and inv.kind == "INVALIDATED"
    assert e.state.value == "WAIT"


def test_tp_alerts_sequence():
    e = Engine()
    now = 1_000.0
    entry = e.step(fixture(), 3891.0, now)
    assert entry is not None and entry.kind == "ENTRY_SELL"
    tp1 = e.step(fixture(), 3880.5, now + 60)   # <= 3881
    assert tp1 is not None and tp1.kind == "TP1"
    assert e.step(fixture(), 3880.5, now + 120) is None  # TP1 tercatat, TP2 belum
    tp2 = e.step(fixture(), 3876.0, now + 180)
    assert tp2 is not None and tp2.kind == "TP2"
    tp3 = e.step(fixture(), 3870.0, now + 240)
    assert tp3 is not None and tp3.kind == "TP3"
    assert e.position is None                  # selesai — posisi tertutup


def test_hold_alert_jangan_open_posisi():
    m5 = M5_PTS[:-1] + [(3891.0, 3891.7, 3890.6, 3891.5)]  # jarum hilang
    e = Engine()
    t0 = 100_000.0
    hold = e.step(fixture(m5_override=m5), 3891.0, now=t0)
    assert hold is not None and hold.kind == "HOLD"
    assert "jangan open posisi" in hold.reasons[0]
    assert e.step(fixture(m5_override=m5), 3891.0, now=t0 + 60) is None  # dedupe
    again = e.step(fixture(m5_override=m5), 3891.0, now=t0 + 1_801)
    assert again is not None and again.kind == "HOLD"


def test_cooldown_expires():
    e = Engine()
    now = 1_000.0
    e.step(fixture(), 3891.0, now)
    assert e.state.value == "COOLDOWN"
    assert e.step(fixture(), 3891.0, now + 60) is None
    sig = e.step(fixture(), 3891.0, now + 46 * 60)
    assert sig is not None and sig.kind == "ENTRY_SELL"


def test_diagnose_full_report():
    d = Engine().diagnose(fixture(), 3891.0)
    assert d["mode"] == "sniper"
    assert d["bias"] == "SELL"
    assert d["trends"] == {"D1": "DOWN", "H4": "DOWN", "H1": "DOWN"}
    assert d["context"] == {"W1": "DOWN"}
    assert all(d["gates"][k]["ok"] for k in
               ("seirama", "veto", "ekstrem", "zona", "sweep",
                "kocokan", "thrust", "trigger", "lastmin"))
    assert d["gates"]["trigger"]["variant"] == "single"
    assert d["plan"]["side"] == "SELL"
    assert d["plan"]["tps"] == [3881.0, 3877.0, 3871.0]


def test_diagnose_missing_data():
    d = Engine().diagnose({}, 3880.0)
    assert d["missing"] and d["plan"] is None
