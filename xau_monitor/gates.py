"""Gate evaluation for the Coach Tom engine — pure, mode-aware, stateless.

Urutan gate (semua harus lolos; berhenti di gate pertama yang gagal):
  seirama -> ekstrem -> veto pola 1/4 -> zona -> sweep -> kocokan -> thrust -> trigger
"""
from __future__ import annotations

from . import patterns as pat
from .candles import TFSnap

ALIGN_TFS = ("D1", "H4", "H1")   # sniper: Daily arah, H4+H1 konfirmasi
CONTEXT_TFS = ("W1",)            # informasi, bukan gate
EXTREME_LOW, EXTREME_HIGH = 20.0, 80.0
MODES = ("sniper", "handgun")


def trend(snap: TFSnap) -> str:
    """Deterministik: posisi di range 20-bar + net 8-bar (0.5xATR)."""
    if len(snap.bars) < 8:
        return "FLAT"
    seg = snap.bars[-8:]
    net = seg[-1].c - seg[0].o
    if snap.pos20 <= 35.0 and net < -0.5 * snap.atr:
        return "DOWN"
    if snap.pos20 >= 65.0 and net > 0.5 * snap.atr:
        return "UP"
    return "FLAT"


def gate_align(snaps: dict[str, TFSnap], mode: str) -> tuple[str | None, list[str]]:
    if mode == "handgun":
        if "M15" not in snaps:
            return None, ["menunggu M15"]
        t = trend(snaps["M15"])
        reasons = [f"M15:{t}"]
        if "M30" in snaps:
            reasons.append(f"M30:{trend(snaps['M30'])}")
        if t == "DOWN":
            return "SELL", reasons + ["mode handgun"]
        if t == "UP":
            return "BUY", reasons + ["mode handgun"]
        return None, reasons + ["M15 belum memberi arah (handgun)"]

    trends = {tf: trend(snaps[tf]) for tf in ALIGN_TFS if tf in snaps}
    reasons = [f"{tf}:{t}" for tf, t in trends.items()]
    for tf in CONTEXT_TFS:
        if tf in snaps:
            reasons.append(f"(konteks {tf}:{trend(snaps[tf])})")
    if len(trends) < len(ALIGN_TFS):
        return None, [f"menunggu TF ({', '.join(sorted(trends))})"] + reasons
    vals = set(trends.values())
    if vals == {"DOWN"}:
        return "SELL", reasons
    if vals == {"UP"}:
        return "BUY", reasons
    return None, reasons + ["TF tidak seirama"]


def veto_chase(snaps: dict[str, TFSnap], bias: str | None) -> str | None:
    """Pola 1/4 (.md): candle besar berjarum pucuk M15 → jangan dilawan."""
    if bias is None:
        return None
    s = snaps.get("M15")
    if not s or len(s.bars) < 2:
        return None
    b = s.bars[-1]
    rng = b.h - b.l
    if rng <= 0:
        return None
    big = rng >= 0.8 * s.atr and abs(b.c - b.o) >= 0.5 * rng
    up_w = (b.h - max(b.o, b.c)) / rng
    dn_w = (min(b.o, b.c) - b.l) / rng
    if bias == "SELL" and b.c > b.o and big and up_w >= 0.35:
        return f"veto pola 1: candle besar naik berjarum atas M15 ({up_w:.0%})"
    if bias == "BUY" and b.c < b.o and big and dn_w >= 0.35:
        return f"veto pola 1: candle besar turun berjarum bawah M15 ({dn_w:.0%})"
    return None


def levels(snaps: dict[str, TFSnap], bias: str, price: float, mode: str,
           digits: int = 2):
    """Zona + TP. Sniper: M15/M30/H1. Handgun: M15 saja."""
    m15 = snaps["M15"]
    sup, res = m15.sup_below, m15.res_above
    if mode == "sniper":
        m30, h1 = snaps.get("M30", m15), snaps.get("H1", m15)
        sup = sup + h1.sup_below[:1]
        res = res + m30.res_above[:1] + h1.res_above[:1]
    if bias == "SELL":
        zones = res
        tps = sorted({round(x, digits) for x in sup if x < price}, reverse=True)[:3]
    else:
        zones = sup
        tps = sorted({round(x, digits) for x in res if x > price})[:3]
    return zones, tps


def conviction(snaps: dict[str, TFSnap], mode: str) -> bool:
    """Conviction (.md:110,125): TF besar seirama. Syarat HOLD, bukan syarat entry."""
    if mode == "handgun":
        return True  # handgun memang scalping TF kecil; arah besar cuma konteks
    trends = [trend(snaps[tf]) for tf in ALIGN_TFS if tf in snaps]
    return len(trends) == len(ALIGN_TFS) and len(set(trends)) == 1


def setup(snaps: dict[str, TFSnap], price: float, mode: str,
          digits: int = 2) -> dict:
    """Susun setup per .md: prasyarat -> pola inti (OR) -> veto kondisional -> modifier.

    Ekstrem bersifat mode-aware (verifikasi transkrip video 21:06 / 22:24 / 24:47):
      - sniper  : D1+H4+H1 arah, ekstrem D1 = konteks arah besar, zona resistance
                  di atas -> jual setelah harga naik (pullback sell).
      - handgun : ekstrem M15 = arah; naik tinggi -> SELL, turun dalam -> BUY.

    Beda dari versi lama (semua gerbang AND):
      - ``sweep`` (penjebolan ganda) = modifier target pendek, BUKAN larangan (.md:81-82).
      - ``veto`` pola 1/4 hanya berlaku bila jarum BELUM tertembus (.md:78-82);
        kalau sudah tertembus, itu justru pola bagus (jebol-jarum) -> jadi trigger.
      - trigger = thrust | jarum tunggal | jarum berurutan | jebol-jarum (OR).
      - ``conviction`` dilaporkan terpisah: syarat hold, bukan syarat entry.
    """
    out: dict = {"bias": None, "why": [], "ok": {}, "zone": None, "zones": [],
                 "tps": [], "details": {}, "missing": []}
    if not all(tf in snaps for tf in ("M15", "M5")):
        out["missing"].append("data candle M15/M5 belum siap")
        return out

    bias, why = gate_align(snaps, mode) if mode == "sniper" else (None, [])
    if mode == "handgun":
        # Handgun = ranging, M1/M5/M15 (.md §6:73). Arah diambil LANGSUNG dari
        # ekstrem M15, bukan dari tren M15. Coach Tom: "sebelum naik diturunkan
        # dulu, sebelum turun dinaikkan dulu" (14:36-15:11); "naik tinggi -> jual"
        # (21:06-21:42, 24:47-25:02).
        # Implementasi lama: M15 UP -> BUY + ekstrem M15 >=80% -> BUY di puncak
        # range (= chasing) dan SELL di dasar range -> 100% ditolak advisor.
        m15 = snaps["M15"]
        if m15.pos20 >= EXTREME_HIGH:
            bias, why = "SELL", [f"M15 naik tinggi {m15.pos20:.1f}% → SELL", "mode handgun"]
        elif m15.pos20 <= EXTREME_LOW:
            bias, why = "BUY", [f"M15 turun dalam {m15.pos20:.1f}% → BUY", "mode handgun"]
        else:
            bias, why = None, [f"M15 belum ekstrem ({m15.pos20:.1f}%) — "
                               "tunggu naik tinggi / turun dalam"]
        out["bias"], out["why"] = bias, why
        # §6:74: handgun tidak mensyaratkan D1/H4+H1 seirama.
        out["ok"]["seirama"] = bias is not None
        out["details"]["conviction"] = conviction(snaps, mode)
        if bias is None:
            return out
        out["ok"]["ekstrem"] = True
        out["details"]["pos_extreme"] = round(m15.pos20, 1)
        out["details"]["extreme_tf"] = "M15"
    else:
        # Sniper: D1+H4+H1 hanya menentukan ARAH. Gate entry = ekstrem M15
        # BERLAWANAN arah — video 21:06 "tunggu dia naik ke resis terdekat...
        # baru kemudian turun, saya ambil sell"; 14:36-15:11 "sebelum naik
        # diturunkan dulu, sebelum turun dinaikkan dulu"; 24:04-24:17 sniper
        # turun ke M15 untuk pola. Versi lama memakai pos20 D1 sehingga bisa
        # BUY di puncak tanpa pullback nyata di TF entry.
        out["bias"], out["why"] = bias, why
        out["ok"]["seirama"] = bias is not None
        out["details"]["conviction"] = conviction(snaps, mode)
        if bias is None:
            return out
        m15 = snaps["M15"]
        extreme = (m15.pos20 >= EXTREME_HIGH if bias == "SELL"
                   else m15.pos20 <= EXTREME_LOW)
        out["ok"]["ekstrem"] = extreme
        out["details"]["pos_extreme"] = round(m15.pos20, 1)
        out["details"]["extreme_tf"] = "M15"
        if not extreme:
            return out

    m15, m5 = snaps["M15"], snaps["M5"]
    zones, tps = levels(snaps, bias, price, mode, digits)
    out["zones"], out["tps"] = zones, tps
    tol = 1.0 * m15.atr
    near = [z for z in zones if abs(price - z) <= tol]
    out["ok"]["zona"] = bool(near)
    out["details"].update(zone=near[0] if near else None, tol=round(tol, digits))
    if not near:
        return out
    zone = near[0]
    out["zone"] = zone

    # Veto pola 1/4 bersifat KONDISIONAL: jarum pucuk baru berbahaya selama belum
    # tertembus. Kalau M5 menunjukkan jarum sudah ditembus lalu close balik,
    # veto batal dan pola berubah jadi jebol-jarum (trigger).
    jebol = pat.jebol_jarum(m5.bars, bias)
    veto = veto_chase(snaps, bias) if jebol is None else None
    if veto and jebol is None:
        out["ok"]["veto"] = False
        out["details"]["veto"] = veto
        return out
    out["ok"]["veto"] = True
    out["details"]["veto"] = None

    # Penjebolan ganda: bukan larangan, tapi isyarat "ambil cuan cepat" (.md:81-82)
    double_break = pat.sweep_both_sides(m15.bars, zone, m15.atr)
    out["ok"]["sweep"] = not double_break
    out["details"]["double_break"] = double_break
    out["details"]["short_target"] = double_break

    kocokan = pat.kocokan_at_zone(m5.bars, zone, tol=0.2 * m5.atr)
    out["ok"]["kocokan"] = kocokan
    thrust = pat.thrust_last(m5.bars, bias, m5.atr)
    out["ok"]["thrust"] = thrust
    single = pat.jarum_single(m5.bars[-1], bias)
    series = pat.jarum_series(m5.bars, bias)

    if jebol is not None:
        variant = "jebol"
    elif thrust:
        variant = "thrust"
    elif single:
        variant = "single"
    elif series:
        variant = "series"
    else:
        variant = "none"

    # Dua jalur entry (.md §6 vs §7): kocokan+dorongan terakhir, ATAU jebol-jarum.
    entry = jebol is not None or (kocokan and variant != "none")
    out["ok"]["trigger"] = entry
    out["details"]["trigger"] = variant if entry else "none"
    if bias == "SELL":
        out["details"]["stop_ref"] = (jebol if jebol is not None else m5.bars[-1].h)
    else:
        out["details"]["stop_ref"] = (jebol if jebol is not None else m5.bars[-1].l)
    return out
