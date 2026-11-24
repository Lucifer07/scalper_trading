"""Coach Tom rule engine: state machine + signal assembly. Pure & testable.

Gate walk lives in gates.py; pola detektor di patterns.py. Di sini hanya:
state (WAIT/COOLDOWN), veto menit-terakhir, perakitan Signal (stop/TP/invalid),
dan laporan diagnose untuk /planning.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from . import gates
from . import patterns as pat
from .candles import Candle, TFSnap

# Re-export untuk kompatibilitas (tests/import lain)
ALIGN_TFS = gates.ALIGN_TFS
CONTEXT_TFS = gates.CONTEXT_TFS
MODES = gates.MODES
trend = gates.trend

# Buffer minimum stop dalam POINT simbol (bukan harga absolut): 50 poin =
# 0,50 USD di XAUUSD (digits=2) dan 5 pip di GBPUSD (digits=5). Angka absolut
# 0,5 hanya benar untuk emas; di FX itu 5000 pip dan membuat stop mustahil.
MIN_BUFFER_POINTS = 50


@dataclass
class Signal:
    kind: str                 # ENTRY_SELL | ENTRY_BUY | INVALIDATED
    price: float
    zone: float
    stop: float
    tps: list[float]
    invalid_level: float
    reasons: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    mode: str = ""            # sniper | handgun (diisi Engine)
    limit_offset: float = 0.0  # geser limit mendekati pasar (lihat Engine)


class State(Enum):
    WAIT = "WAIT"
    COOLDOWN = "COOLDOWN"


class Engine:
    """WAIT -> (semua gate lolos) -> COOLDOWN -> WAIT. INVALIDATED -> WAIT."""

    def __init__(self, cooldown_minutes: int = 45, mode: str = "sniper",
                 hold_cooldown_minutes: int = 30, digits: int = 2,
                 limit_offset_pct: float = 2.0,
                 limit_offset_max_pips: float = 10.0) -> None:
        if mode not in MODES:
            raise ValueError(f"mode harus salah satu dari {MODES}, bukan {mode!r}")
        if not 0 <= digits <= 8:
            raise ValueError(f"digits broker tidak valid: {digits!r}")
        self.mode = mode
        self.digits = int(digits)
        self.point = 10.0 ** -self.digits
        # Offset limit: 2% range candle M5 terakhir, dibatasi N pip (default 10).
        self._offset_pct = max(0.0, float(limit_offset_pct))
        self._offset_max = max(0.0, float(limit_offset_max_pips)) * self._pip()
        self.cooldown = cooldown_minutes * 60
        self.hold_cooldown = hold_cooldown_minutes * 60
        self.state = State.WAIT
        self.last: Signal | None = None
        self.position: dict | None = None
        self._cooldown_until = 0.0
        self._last_hold: dict[float, float] = {}
        self._last_reason = ""

    def _pip(self) -> float:
        """1 pip = 10 point untuk simbol 3/5 digit, 1 point lainnya (MT4)."""
        return self.point * (10 if self.digits in (3, 5) else 1)

    def _limit_offset(self, m5: TFSnap) -> float:
        """Geser limit mendekati pasar: 2% range candle M5 terakhir, cap N pip."""
        if not m5.bars:
            return 0.0
        b = m5.bars[-1]
        rng = max(0.0, b.h - b.l)
        return min(rng * self._offset_pct / 100.0, self._offset_max)

    def _invalid_level(self, snaps: dict[str, TFSnap], bias: str,
                       zone: float) -> float:
        m15 = snaps["M15"]
        if self.mode == "sniper":
            h1 = snaps.get("H1", m15)
            refs = h1.res_above[:1] if bias == "SELL" else h1.sup_below[:1]
        else:
            refs = []
        if bias == "SELL":
            return max(zone, *(refs or [zone])) + 2.0 * m15.atr
        return min(zone, *(refs or [zone])) - 2.0 * m15.atr

    def min_buffer(self) -> float:
        """Buffer stop minimum dalam satuan harga simbol (50 point, bukan 0,5 absolut)."""
        return MIN_BUFFER_POINTS * self.point

    def _r(self, value: float) -> float:
        """Bulatkan level sesuai presisi broker (XAUUSD 2, GBPUSD 5)."""
        return round(value, self.digits)

    def evaluate(self, snaps: dict[str, TFSnap], price: float,
                 now: float | None = None,
                 forming_m15: "Candle | None" = None) -> Signal | None:
        s = gates.setup(snaps, price, self.mode, digits=self.digits)
        if s["missing"]:
            self._last_reason = "; ".join(s["missing"])
            return None
        if not s["ok"].get("seirama"):
            self._last_reason = "; ".join(s["why"])
            return None
        bias = s["bias"]
        if not s["ok"].get("ekstrem"):
            pos = s["details"].get("pos_extreme")
            tf = s["details"].get("extreme_tf", "M15")
            # Entry melawan arah: BUY butuh M15 turun dalam, SELL butuh naik tinggi.
            need_low = bias == "BUY"
            self._last_reason = (
                f"belum {'turun dalam' if need_low else 'naik tinggi'}"
                f" ({tf} pos {pos}%, butuh {'<=20%' if need_low else '>=80%'})")
            return None
        if s["details"].get("veto"):
            self._last_reason = s["details"]["veto"]
            return None
        if not s["ok"].get("zona"):
            self._last_reason = f"harga belum kembali ke zona {'resis' if bias == 'SELL' else 'sup'}"
            return None
        # Jebol ganda BUKAN larangan (.md:81-82): hanya mengecilkan target & larangan hold.
        if not s["ok"].get("trigger"):
            self._last_reason = ("pola inti belum lengkap: butuh kocokan+dorongan terakhir "
                                 "atau jebol-jarum di M5")
            return None
        if pat.last_minutes_full(forming_m15, bias, now):
            self._last_reason = "M15 masih full melawan bias di menit terakhir — jangan lawan"
            return None

        m1 = snaps.get("M1", snaps["M5"])
        buffer = 0.35 * max(m1.atr, self.min_buffer())
        ref = float(s["details"]["stop_ref"])
        zone = float(s["zone"])
        # Level struktur (jarum/jebol) bisa sudah tertinggal bila harga bergerak
        # melewatinya sebelum candle close — stop jadi di sisi salah dan order
        # ditolak `intent_invalid`. Ambil sisi yang lebih aman dari struktur vs
        # harga saat sinyal: BUY -> min, SELL -> max (stop tetap di sisi rugi).
        anchor = min(ref, price) if bias == "BUY" else max(ref, price)
        stop = anchor + buffer if bias == "SELL" else anchor - buffer
        reasons = s["why"] + [f"zona {zone:.{self.digits}f}",
                              f"trigger {s['details']['trigger']}",
                              f"conviction {'ya' if s['details'].get('conviction') else 'tidak'}"]
        if s["details"].get("short_target"):
            reasons.append("jebol ganda: target pendek, jangan hold")
        return Signal(f"ENTRY_{bias}", price, self._r(zone), self._r(stop),
                      s["tps"], self._r(self._invalid_level(snaps, bias, zone)),
                      reasons, limit_offset=self._limit_offset(snaps["M5"]))

    def diagnose(self, snaps: dict[str, TFSnap], price: float,
                 now: float | None = None,
                 forming_m15: "Candle | None" = None) -> dict:
        s = gates.setup(snaps, price, self.mode, digits=self.digits)
        out: dict = {"mode": self.mode, "bias": s["bias"], "trends": {},
                     "context": {}, "gates": {}, "plan": None, "missing": s["missing"]}
        if s["missing"]:
            return out
        out["conviction"] = s["details"].get("conviction")
        out["short_target"] = s["details"].get("short_target", False)
        ref = snaps.get("M15")
        if ref is not None:
            out["extreme_tf"] = "M15"
            out["extreme_pos"] = round(ref.pos20, 1)
        m15 = snaps.get("M15")
        if m15 is not None:
            out["watch"] = {"res_above": [self._r(x) for x in m15.res_above[:2]],
                            "sup_below": [self._r(x) for x in m15.sup_below[:2]]}
        if self.mode == "sniper":
            out["trends"] = {tf: gates.trend(snaps[tf])
                             for tf in ALIGN_TFS if tf in snaps}
            out["context"] = {tf: gates.trend(snaps[tf])
                              for tf in CONTEXT_TFS if tf in snaps}
        else:
            out["trends"] = {tf: gates.trend(snaps[tf])
                             for tf in ("M15", "M30") if tf in snaps}
        g = s["ok"]
        out["gates"] = {
            "seirama": {"ok": g.get("seirama", False)},
            "veto": {"ok": g.get("veto", True), "reason": s["details"].get("veto")},
            "ekstrem": {"ok": g.get("ekstrem", False),
                        "pos": s["details"].get("pos_extreme"),
                        "tf": s["details"].get("extreme_tf")},
            "zona": {"ok": g.get("zona", False), "zone": s["details"].get("zone"),
                     "tol": s["details"].get("tol")},
            "sweep": {"ok": g.get("sweep", False),
                      "short_target": s["details"].get("short_target", False)},
            "kocokan": {"ok": g.get("kocokan", False)},
            "thrust": {"ok": g.get("thrust", False)},
            "trigger": {"ok": g.get("trigger", False),
                        "variant": s["details"].get("trigger", "none")},
            "lastmin": {"ok": not pat.last_minutes_full(forming_m15, s["bias"], now)},
        }
        # Kandidat level per mode (handgun M15 saja; sniper + M30/H1) dipakai
        # panel untuk menghitung "harga trigger" walau harga belum di zona.
        out["zones"] = [self._r(z) for z in s["zones"]]
        # Gate yang benar-benar dievaluasi: rantai `setup` berhenti di kegagalan
        # pertama, jadi sisanya masih default dan tidak boleh dihitung lolos.
        out["evaluated"] = list(s["ok"].keys())
        if s["bias"] is not None:
            out["evaluated"].append("lastmin")
        if s["bias"] and s["zone"] is not None:
            m5 = snaps["M5"]
            m1 = snaps.get("M1", m5)
            buffer = 0.35 * max(m1.atr, self.min_buffer())
            ref = s["details"].get("stop_ref")
            if ref is None:
                ref = m5.bars[-1].h if s["bias"] == "SELL" else m5.bars[-1].l
            # Samakan dengan evaluate(): stop harus di sisi benar dari harga.
            anchor = min(ref, price) if s["bias"] == "BUY" else max(ref, price)
            stop = anchor + buffer if s["bias"] == "SELL" else anchor - buffer
            out["plan"] = {"side": s["bias"], "zone": self._r(s["zone"]),
                           "stop": self._r(stop), "tps": s["tps"],
                           "invalid": self._r(self._invalid_level(
                               snaps, s["bias"], s["zone"]))}
        return out

    def _manage_position(self, price: float) -> Signal | None:
        """Alert manajemen posisi: TP tercapai (close manual) / invalidasi (close now)."""
        pos = self.position
        if not pos:
            return None
        side = pos["side"]
        breach = (side == "SELL" and price > pos["invalid"]) or \
                 (side == "BUY" and price < pos["invalid"])
        if breach:
            self.position = None
            self.state, self.last = State.WAIT, None
            return Signal("INVALIDATED", price, pos["zone"], 0, [], pos["invalid"],
                          ["harga menembus level invalidasi — CLOSE MANUAL sekarang"])
        for idx, tp in enumerate(pos["tps"]):
            if idx in pos["hits"]:
                continue
            reached = price <= tp if side == "SELL" else price >= tp
            if not reached:
                break
            pos["hits"].add(idx)
            sig = Signal(f"TP{idx + 1}", price, pos["zone"], pos["stop"], [tp],
                         pos["invalid"],
                         [f"target {tp:.{self.digits}f} tercapai — close manual (jangan serakah)"])
            if len(pos["hits"]) >= len(pos["tps"]):
                self.position = None
            return sig
        return None

    def _maybe_hold(self, snaps: dict[str, TFSnap], price: float,
                    now: float) -> Signal | None:
        """Alert HOLD: jangan open posisi saat pola ideal belum lengkap."""
        if self.position:
            return None
        s = gates.setup(snaps, price, self.mode)
        if s["missing"] or not s["ok"].get("seirama") or not s["ok"].get("ekstrem"):
            return None
        if not s["ok"].get("zona") or s["zone"] is None:
            return None
        if s["ok"].get("trigger"):
            return None
        if s["details"].get("short_target"):
            why = "zona rawan jebol ganda — kalau entry, target pendek & jangan hold"
        elif not s["ok"].get("kocokan"):
            why = "belum ada kocokan di zona"
        else:
            why = "kocokan ada, tapi dorongan terakhir / jebol-jarum belum muncul"
        zone = float(s["zone"])
        if now - self._last_hold.get(zone, 0.0) < self.hold_cooldown:
            return None
        self._last_hold[zone] = now
        return Signal("HOLD", price, self._r(zone), 0, [], 0,
                      [f"jangan open posisi — {why}",
                       f"bias {s['bias']} menunggu pola ideal di zona "
                       f"{zone:.{self.digits}f}"])

    def step(self, snaps: dict[str, TFSnap], price: float, now: float | None = None,
             forming_m15: "Candle | None" = None) -> Signal | None:
        now = now if now is not None else time.time()
        if self.state is State.COOLDOWN and now >= self._cooldown_until:
            self.state, self.last = State.WAIT, None

        managed = self._manage_position(price)
        if managed is not None:
            managed.mode = self.mode
            return managed

        sig = self.evaluate(snaps, price, now=now, forming_m15=forming_m15)
        if sig is not None:
            sig.mode = self.mode
        if sig is not None and self.state is State.WAIT:
            self.state = State.COOLDOWN
            self._cooldown_until = now + self.cooldown
            self.last = sig
            self.position = {
                "side": "SELL" if sig.kind == "ENTRY_SELL" else "BUY",
                "zone": sig.zone, "stop": sig.stop, "tps": list(sig.tps),
                "invalid": sig.invalid_level, "hits": set(),
            }
            return sig
        held = self._maybe_hold(snaps, price, now)
        if held is not None:
            held.mode = self.mode
        return held

    @property
    def blocked_reason(self) -> str:
        return self._last_reason
