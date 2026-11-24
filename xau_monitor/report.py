"""Telegram message formatting (HTML). Pure functions, no I/O."""
from __future__ import annotations

import html
from datetime import datetime

from . import readiness
from .strategy import Engine, Signal


def _now() -> str:
    return datetime.now().strftime("%d-%m %H:%M")


def _dur(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}j {m}m" if h else f"{m}m"


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def _px(value: float, digits: int) -> str:
    """Format harga sesuai presisi broker (XAUUSD 2, GBPUSD 5)."""
    return f"{value:.{max(0, int(digits))}f}"


def fmt_signal(sig: Signal, symbol: str, tick_rate: float,
               digits: int = 2) -> str:
    s = html.escape(symbol)
    px = lambda v: _px(v, digits)
    mode = f" · {html.escape(sig.mode)}" if sig.mode else ""
    if sig.kind == "HOLD":
        return (f"⏸️ <b>{s} — HOLD{mode}, jangan open posisi</b>\n"
                f"Harga {px(sig.price)} · zona {px(sig.zone)}\n"
                f"{' · '.join(sig.reasons)}\n⏱ {_now()} WIB")
    if sig.kind.startswith("TP"):
        return (f"🎯 <b>{s} — {sig.kind} tercapai (close manual)</b>\n"
                f"Harga {px(sig.price)} · target {px(sig.tps[0])}\n"
                f"{' '.join(sig.reasons)}\n⏱ {_now()} WIB")
    if sig.kind == "INVALIDATED":
        return (f"⚠️ <b>{s} — BIAS INVALID</b>\n"
                f"Harga {px(sig.price)} menembus level {px(sig.invalid_level)}\n"
                f"{' '.join(sig.reasons)}\n⏱ {_now()} WIB")
    arrow = "🔻 SELL" if sig.kind == "ENTRY_SELL" else "🔺 BUY"
    where = "di resistance" if "SELL" in sig.kind else "di support"
    tps = " · ".join(px(t) for t in sig.tps) or "—"
    return (
        f"{arrow} <b>{s}{mode} — pola jarum {where}</b>\n"
        f"Harga live: <b>{px(sig.price)}</b>  (zona {px(sig.zone)})\n"
        f"Cut loss : {px(sig.stop)}\n"
        f"Target   : {tps}\n"
        f"Invalidasi: {px(sig.invalid_level)}\n"
        f"<pre>{' · '.join(sig.reasons)}</pre>\n"
        f"⏱ {_now()} WIB · {tick_rate:.0f} tick/s"
    )


def fmt_planning(symbol: str, price: float, engine: Engine, diag: dict,
                 digits: int = 2) -> str:
    px = lambda v: _px(v, digits)
    head = (f"🧭 <b>{html.escape(symbol)} — PLANNING</b> (mode {engine.mode})\n"
            f"Harga <b>{px(price)}</b> · state {engine.state.value}\n"
            f"{'─' * 22}\n")
    if diag.get("missing"):
        return head + f"⏳ {html.escape('; '.join(diag['missing']))}\n⏱ {_now()} WIB"

    t = diag["trends"]
    ctx = diag.get("context") or {}
    g = diag["gates"]
    trend_txt = " · ".join(f"{k} {v}" for k, v in t.items())
    if ctx:
        trend_txt += " (" + " · ".join(f"konteks {k} {v}" for k, v in ctx.items()) + ")"

    lines = [head]
    evaluated = diag["bias"] is not None

    def row(key: str, label: str, extra: str = "") -> str:
        if not evaluated:
            return f"— <b>{label}</b> — belum dievaluasi"
        return f"{_mark(g[key]['ok'])} <b>{label}</b>{extra}"

    lines.append(f"{_mark(g['seirama']['ok'])} <b>Gate 1 · TF seirama</b>\n   {trend_txt}")

    pos = g["ekstrem"].get("pos")
    tf_ex = g["ekstrem"].get("tf") or ("D1" if engine.mode == "sniper" else "M15")
    if isinstance(pos, (int, float)):
        lines.append(f"{_mark(g['ekstrem']['ok'])} <b>Gate 2 · ekstrem {tf_ex}</b> — "
                     f"pos {pos:.1f}% (≤20% sell / ≥80% buy)")
    else:
        lines.append(f"— <b>Gate 2 · ekstrem {tf_ex}</b> — bias belum ada")

    v = g["veto"]
    veto_txt = html.escape(v["reason"]) if v.get("reason") else "bersih"
    lines.append(f"{_mark(v['ok'])} <b>Veto pola 1/4</b> — {veto_txt}")

    z = g["zona"]
    if isinstance(z.get("zone"), (int, float)):
        lines.append(f"{_mark(z['ok'])} <b>Gate 3 · zona</b> — {px(z['zone'])} "
                     f"(toleransi {px(z['tol'])})")
    else:
        lines.append("— <b>Gate 3 · zona</b> — belum ada level")
    if evaluated and g["sweep"].get("short_target"):
        lines.append("⚠️ <b>Jebol ganda</b> — bukan larangan; target pendek, jangan hold")
    else:
        lines.append(row("sweep", "Zona bersih (6 candle M15)"))
    lines.append(row("kocokan", "Kocokan di zona"))
    lines.append(f"{_mark(g['kocokan']['ok'])} <b>Kocokan di zona</b>")
    lines.append(f"{_mark(g['trigger']['ok'])} <b>Pola inti</b> — "
                 f"{g['trigger']['variant']} (kocokan+dorongan / jebol-jarum)")
    conv = diag.get("conviction")
    lines.append(f"{'✅' if conv else '➖'} <b>Conviction</b> — "
                 f"{'semua TF seirama (boleh hold)' if conv else 'TF tidak seirama (jangan hold)'}")
    lines.append(f"{_mark(g['lastmin']['ok'])} <b>Menit terakhir M15</b> — "
                 f"{'aman' if g['lastmin']['ok'] else 'masih full melawan bias'}")
    lines.append("─" * 22)

    rd = readiness.of_diag(engine.mode, diag, price, digits=digits)
    lines.append(readiness.fmt_line(rd))
    lines.append("─" * 22)

    plan = diag.get("plan")
    if plan:
        tps = " / ".join(px(x) for x in plan["tps"]) or "—"
        lines.append(f"🎯 <b>Rencana bila semua lolos</b>\n"
                     f"  {plan['side']} zona {px(plan['zone'])}\n"
                     f"  CL {px(plan['stop'])} · TP {tps}\n"
                     f"  Invalidasi {px(plan['invalid'])}")
    else:
        lines.append("🎯 <b>Rencana</b>: tunggu gate di atas terpenuhi dulu.")
    lines.append(f"⏱ {_now()} WIB")
    return "\n".join(lines)


def fmt_market_guard(symbol: str, dead: bool, flat_bars: int, price: float,
                     digits: int = 2) -> str:
    """Pesan transisi dead-market guard (jeda/pulih evaluasi sinyal)."""
    s = html.escape(symbol)
    if dead:
        return (f"🔒 <b>{s} — market dianggap tutup/datar</b>\n"
                f"Harga beku di {_px(price, digits)} selama ≥{flat_bars} bar M1 "
                f"tanpa rentang\n"
                f"Evaluasi sinyal &amp; alert dijeda sampai harga bergerak lagi\n"
                f"⏱ {_now()} WIB")
    return (f"🔓 <b>{s} — market aktif kembali</b>\n"
            f"Harga bergerak lagi ({_px(price, digits)}) — evaluasi dilanjutkan\n"
            f"⏱ {_now()} WIB")


def fmt_help(symbol: str) -> str:    return (f"🤖 <b>{html.escape(symbol)} monitor</b>\n"
            f"/status — kondisi live (harga, state, feed)\n"
            f"/planning — target & alasan gate yang belum terpenuhi\n"
            f"Sinyal SELL/BUY dikirim otomatis saat 4 gate lolos.")


def fmt_status_all(symbol: str, price: float, engines: dict, broker, rate: float,
                   uptime: float, digits: int = 2) -> str:
    """Satu pesan status untuk semua mode yang berjalan (feed MT4)."""
    ticks = broker.tick_count if broker else 0
    age = broker.age if broker else float("inf")
    age_txt = "—" if age == float("inf") else f"{age:.0f}s"
    lines = [f"ℹ️ <b>{html.escape(symbol)} — STATUS</b>",
             f"Harga: <b>{_px(price, digits)}</b> · uptime {_dur(uptime)}",
             f"Feed MT4: {ticks} tick · {rate:.0f}/s · umur {age_txt}"]
    for mode, eng in engines.items():
        lines.append(f"{html.escape(mode)}: state <b>{eng.state.value}</b> — "
                     f"{html.escape(eng.blocked_reason or '-')}")
    lines.append(f"⏱ {_now()} WIB")
    return "\n".join(lines)
