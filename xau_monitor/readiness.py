"""Kesiapan trigger per mode: berapa persen gate sudah lolos + harga targetnya.

Kontrak: modul murni atas `Engine.diagnose()` (tanpa I/O). Dipakai tiga tempat:
laporan Telegram `/planning`, panel GUI (status JSON), dan tes.

"Persen kesiapan" = gate entry yang sudah lolos / gate entry wajib. Yang dihitung
hanya gate yang benar-benar memblokir entry (lihat REQUIRED) — `sweep` adalah
modifier target, `thrust` salah satu varian pola, bukan syarat terpisah.
"""
from __future__ import annotations

REQUIRED = ("seirama", "ekstrem", "veto", "zona", "kocokan", "trigger", "lastmin")
LABELS = {
    "seirama": "TF seirama",
    "ekstrem": "ekstrem harga",
    "veto": "veto pola 1/4",
    "zona": "zona resis/sup",
    "kocokan": "kocokan di zona",
    "thrust": "dorongan terakhir",
    "trigger": "pola inti",
    "lastmin": "menit terakhir M15",
}


def of_diag(mode: str, diag: dict, price: float, digits: int = 2) -> dict:
    gates = diag.get("gates") or {}
    evaluated = set(diag.get("evaluated") or gates.keys())
    passed = [g for g in REQUIRED if g in evaluated and gates.get(g, {}).get("ok")]
    percent = round(len(passed) / len(REQUIRED) * 100, 1)

    next_gate, next_label = _next_blocker(gates, evaluated)
    target, side, dist_usd, dist_pct = _target(diag, price)
    plan = diag.get("plan")
    return {
        "mode": mode,
        "percent": percent,
        "remaining_pct": round(100.0 - percent, 1),
        "gates_passed": len(passed),
        "gates_total": len(REQUIRED),
        "next_gate": next_gate,
        "next_label": next_label,
        "side": side,
        "trigger_price": target,
        "distance_usd": round(dist_usd, digits) if dist_usd is not None else None,
        "distance_pct": round(dist_pct, 2) if dist_pct is not None else None,
        "extreme": _extreme(diag, mode),
        "watch": diag.get("watch") or {},
        "zones": diag.get("zones") or [],
        "zone_tol": (gates.get("zona") or {}).get("tol"),
        "plan": plan,
        "digits": max(0, int(digits)),
        "note": _note(diag, gates, percent, target, digits),
    }


def _extreme(diag: dict, mode: str) -> dict | None:
    """Posisi harga di TF entry (M15) vs ambang gate ekstrem.

    Berlawanan arah (video 14:36-15:11 / 21:06): BUY butuh turun dalam (≤20%),
    SELL butuh naik tinggi (≥80%) — beda untuk dua mode.
    """
    pos = diag.get("extreme_pos")
    if pos is None:
        return None
    tf = diag.get("extreme_tf") or "M15"
    bias = diag.get("bias")
    need = 20.0 if bias == "BUY" else 80.0 if bias == "SELL" else None
    if need is None:
        gap, ok = None, None
    elif bias == "BUY":
        gap, ok = round(max(0.0, pos - need), 1), pos <= need
    else:
        gap, ok = round(max(0.0, need - pos), 1), pos >= need
    return {"tf": tf, "pos": pos, "need": need, "gap": gap, "ok": ok}


def _next_blocker(gates: dict, evaluated: set[str]) -> tuple[str | None, str]:
    # Prioritaskan gate yang sudah dievaluasi tapi gagal (rantai berhenti di sini),
    # lalu gate berikutnya yang belum dievaluasi.
    for gate in REQUIRED:
        if gate in evaluated and not gates.get(gate, {}).get("ok"):
            return gate, LABELS[gate]
    for gate in REQUIRED:
        if gate not in evaluated:
            return gate, LABELS[gate]
    return None, "siap"


def _target(diag: dict, price: float) -> tuple[float | None, str | None, float | None,
                                                float | None]:
    bias = diag.get("bias")
    if not price:
        return None, bias, None, None
    plan = diag.get("plan") or {}
    if plan.get("zone"):
        target = float(plan["zone"])
        return target, bias or plan.get("side"), target - price, (target - price) / price * 100
    zones = [float(z) for z in (diag.get("zones") or []) if z]
    if not bias or not zones:
        return None, bias, None, None
    if bias == "SELL":
        above = [z for z in zones if z >= price]
        target = min(above) if above else min(zones, key=lambda z: abs(z - price))
    else:
        below = [z for z in zones if z <= price]
        target = max(below) if below else min(zones, key=lambda z: abs(z - price))
    return target, bias, target - price, (target - price) / price * 100


def _note(diag: dict, gates: dict, percent: float, target: float | None,
          digits: int = 2) -> str:
    watch = _watch_text(diag.get("watch"), digits)
    if not diag.get("bias"):
        return ("bias belum ada — gate TF seirama belum lolos"
                + (f" · pantau {watch}" if watch else ""))
    if target is None:
        return ("belum ada level zona untuk bias ini"
                + (f" · pantau {watch}" if watch else ""))
    if percent >= 100.0:
        return "semua gate lolos — siap trigger"
    if gates.get("zona", {}).get("ok"):
        return "harga di zona — tunggu pola inti (kocokan/dorongan atau jebol-jarum)"
    return "tunggu harga masuk zona resis/sup"


def _watch_text(watch: dict | None, digits: int = 2) -> str:
    if not watch:
        return ""
    parts = []
    if watch.get("res_above"):
        parts.append(f"res {watch['res_above'][0]:.{digits}f}")
    if watch.get("sup_below"):
        parts.append(f"sup {watch['sup_below'][0]:.{digits}f}")
    return " / ".join(parts)


def fmt_line(rd: dict) -> str:
    """Satu baris untuk laporan Telegram (HTML)."""
    mode = str(rd.get("mode", "")).upper()
    pct = rd.get("percent", 0.0)
    left = rd.get("remaining_pct", 0.0)
    gates = f"{rd.get('gates_passed', 0)}/{rd.get('gates_total', 0)} gate"
    target = rd.get("trigger_price")
    if target is None:
        where = "target — (belum ada level)"
    else:
        dist = rd.get("distance_pct")
        sign = "+" if (dist or 0) > 0 else ""
        arah = "naik" if (dist or 0) > 0 else "turun" if (dist or 0) < 0 else "di harga"
        d = rd.get("digits", 2)
        where = (f"target <b>{target:.{d}f}</b> ({sign}{dist:.2f}% · {arah})")
    nxt = rd.get("next_label") or "siap"
    return (f"🎯 <b>{mode}</b> — kesiapan {pct:.0f}% · kurang {left:.0f}% · {gates}\n"
            f"   {where} · berikutnya: {nxt}")
