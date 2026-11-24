"""Eksekusi order: Signal -> intent -> advisor -> guard -> EA MT4 -> audit.

Setiap langkah bisa menolak; penolakan bukan exception yang mematikan monitor,
tapi laporan ke Telegram + baris audit. Satu-satunya efek samping ke broker
adalah `order_send` dan itu hanya terjadi setelah guard deterministik lolos.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from dataclasses import dataclass

from .advisor import (
    Advisor, AdvisorContext, AdvisorRejected, AdvisorUnavailable, apply_decision,
)
from .mt4_link import Mt4Error, Mt4Link, Mt4Rejected
from .orders import (
    AccountState, GuardRejection, OrderIntent, RiskConfig, SymbolSpec, build_plan,
)
from .strategy import Signal
from .trailing import Trailer

log = logging.getLogger("executor")


@dataclass(frozen=True)
class ExecConfig:
    symbol: str
    magic: int
    deviation_points: int
    dedupe_seconds: int
    band_atr: float
    log_path: str
    max_drift: float = 1.0
    sibling_magics: tuple[int, ...] = ()
    max_positions: int = 2
    pending_ttl_s: float = 300.0
    trail_retrace: float = 0.20
    layering_enabled: bool = False
    layering_min: int = 2
    layering_margin_safety: float = 0.9
    layering_max_margin_pct: float = 2.0


class OrderExecutor:
    def __init__(self, link: Mt4Link, advisor: Advisor, risk: RiskConfig,
                 cfg: ExecConfig) -> None:
        self._link = link
        self._advisor = advisor
        self._risk = risk
        self._cfg = cfg
        self._recent: dict[str, float] = {}
        self.layering = cfg.layering_enabled   # bisa di-toggle runtime dari panel
        self._trailer = Trailer(link, cfg.symbol, cfg.magic, self._audit,
                                cfg.pending_ttl_s, cfg.trail_retrace)

    # ---- public ----------------------------------------------------
    def execute(self, sig: Signal, atr_m1: float,
                metrics: tuple[tuple[str, float], ...] = ()) -> str:
        try:
            return self._execute(sig, atr_m1, metrics)
        except Exception as exc:  # noqa: BLE001 - order gagal tidak boleh mematikan monitor
            log.exception("eksekusi order gagal tak terduga")
            self._audit({"event": "error", "code": type(exc).__name__, "detail": str(exc)})
            return self._msg("⚠️", "ORDER ERROR", f"{type(exc).__name__}: {exc}")

    def poll(self) -> list[str]:
        """Cek pending (expiry/fill) + trailing TP; dipanggil loop monitor."""
        return self._trailer.poll()

    def has_position(self) -> bool:
        """Apakah mode ini punya posisi terbuka di broker (bukan rencana engine)."""
        return self._trailer.has_active()

    # ---- pipeline --------------------------------------------------
    def _execute(self, sig: Signal, atr_m1: float,
                 metrics: tuple[tuple[str, float], ...] = ()) -> str:
        side = "BUY" if sig.kind == "ENTRY_BUY" else "SELL"
        key = self._dedupe_key(sig, side)
        if self._is_duplicate(key):
            return self._msg("⏳", "SINYAL SAMA (dedupe)",
                             f"{side} zona {sig.zone:g} sudah diproses "
                             f"{self._cfg.dedupe_seconds}s terakhir")

        try:
            # Saat layering ON, batas posisi diganti batas margin (keputusan pemilik).
            if not self.layering:
                total_open = self._total_positions()
                if total_open >= self._cfg.max_positions:
                    return self._msg("⏳", "BATAS POSISI",
                                     f"{total_open}/{self._cfg.max_positions} posisi terbuka "
                                     f"lintas mode — tidak menambah")
            open_positions, spec, account = self._broker_snapshot()
        except Mt4Error as exc:
            self._audit({"event": "ea_unavailable", "side": side, "detail": str(exc)})
            return self._msg("📡", "BRIDGE MT4 GAGAL", str(exc))
        except GuardRejection as exc:
            self._audit({"event": "guard_reject", "side": side, "code": exc.code,
                         "detail": exc.detail})
            return self._msg("🛑", "ORDER DITOLAK GUARD", f"{exc.code}: {exc.detail}")
        if open_positions:
            ticket = open_positions[0]["ticket"]
            return self._msg("⏳", "POSISI SUDAH TERBUKA",
                             f"ticket #{ticket} — satu posisi per magic, tidak menambah")

        # Quote sebelum advisor: advisor LLM bisa 20-50s, harga bisa lari.
        entry_then = spec.ask if side == "BUY" else spec.bid
        ctx = AdvisorContext(
            symbol=self._cfg.symbol, side=side, mode=sig.mode or sig.kind,
            price=sig.price, zone=sig.zone, stop=sig.stop, tps=tuple(sig.tps),
            invalid=sig.invalid_level, atr_m1=atr_m1, reasons=tuple(sig.reasons),
            broker_protection=self._risk.attach_sl or self._risk.attach_tp,
            metrics=metrics, digits=spec.digits,
            balance=account.balance, equity=account.equity,
            free_margin=account.free_margin,
            margin_per_lot=spec.margin_per_lot,
            lot=min(self._risk.max_lot, spec.lot_max), layering=self.layering,
            lot_min=spec.lot_min, lot_step=spec.lot_step,
            layering_min=self._cfg.layering_min,
            margin_safety=self._cfg.layering_margin_safety,
            max_margin_pct=self._cfg.layering_max_margin_pct,
        )

        try:
            decision = self._advisor.review(ctx)
        except AdvisorUnavailable as exc:
            self._audit({"event": "advisor_unavailable", "side": side, "detail": str(exc)})
            return self._msg("🤖", "AI TIDAK TERSEDIA — TIDAK ORDER", str(exc))

        try:
            levels = apply_decision(ctx, decision, self._cfg.band_atr)
        except AdvisorRejected as exc:
            self._audit({"event": "advisor_reject", "side": side,
                         "rationale": str(exc), "advisor": self._advisor.name})
            return self._msg("🤖", f"AI MENOLAK ({self._advisor.name})", str(exc))

        intent = OrderIntent(
            symbol=self._cfg.symbol, side=side, ref_price=sig.price,
            stop=levels.stop, tps=levels.tps, invalid=sig.invalid_level,
            zone=sig.zone, mode=sig.kind, atr_m1=atr_m1,
        )
        try:
            spec_now = SymbolSpec.from_wire(self._link.symbol_info(self._cfg.symbol))
        except Mt4Error as exc:
            self._audit({"event": "ea_unavailable", "side": side, "detail": str(exc)})
            return self._msg("📡", "BRIDGE MT4 GAGAL", str(exc))
        entry_now = spec_now.ask if side == "BUY" else spec_now.bid
        drift = entry_now - entry_then
        if abs(drift) > self._cfg.max_drift:
            self._audit({"event": "guard_reject", "side": side, "code": "price_moved",
                         "detail": f"drift {drift:+.{spec_now.digits}f} selama review AI"})
            return self._msg("🛑", "ORDER DITOLAK GUARD",
                             f"price_moved: harga bergerak {drift:+.{spec_now.digits}f} "
                             f"selama review AI "
                             f"(batas {self._cfg.max_drift})")

        try:
            plan = build_plan(intent, spec_now, account, self._risk,
                              lots_requested=levels.lots)
        except GuardRejection as exc:
            self._audit({"event": "guard_reject", "side": side, "code": exc.code,
                         "detail": exc.detail})
            return self._msg("🛑", "ORDER DITOLAK GUARD", f"{exc.code}: {exc.detail}")

        # ---- layering: AI menentukan jumlah, guard batasi margin ----
        layers = 1
        if self.layering:
            want = max(self._cfg.layering_min, int(getattr(decision, "layers", 0) or 0))
            per_layer = spec_now.margin_per_lot * plan.lots
            free_cap = (int(account.free_margin * self._cfg.layering_margin_safety
                            // per_layer) if per_layer > 0 else 0)
            # Batas total eksposur: margin batch <= X% equity (bukan batas jumlah layer).
            exposure_cap = (int(account.equity * self._cfg.layering_max_margin_pct
                                / 100.0 // per_layer) if per_layer > 0 else 0)
            layers = min(want, free_cap, exposure_cap)
            if layers < self._cfg.layering_min:
                self._audit({"event": "layering_reject", "side": side,
                             "detail": f"free_margin {account.free_margin:.2f}, "
                                       f"exposure_cap {exposure_cap}, per_layer "
                                       f"{per_layer:.2f}"})
                return self._msg("🛑", "LAYERING DITOLAK",
                                 f"tidak cukup untuk {self._cfg.layering_min} layer "
                                 f"(free {account.free_margin:.2f}, eksposur "
                                 f"{self._cfg.layering_max_margin_pct:g}% = "
                                 f"{exposure_cap} layer, per layer {per_layer:.2f})")

        self._recent[key] = time.time()  # sebelum kirim: timeout pun tidak boleh dobel
        # Entry = pending LIMIT di level stop engine (SL/TP broker dikosongkan).
        # plan.stop bisa None saat ATTACH_SL=0, jadi level dihitung dari intent.
        # Offset (2% range candle M5 terakhir, cap 10 pip) menggeser limit
        # MENDEKATI pasar: BUY +offset, SELL -offset, supaya lebih mudah terisi.
        base = intent.stop + plan.basis
        off = max(0.0, float(getattr(sig, "limit_offset", 0.0)))
        stops = max(spec_now.stops_level_points * spec_now.point, spec_now.point)
        if side == "BUY":
            first = max(min(base + off, spec_now.ask - stops), base)
        else:
            first = min(max(base - off, spec_now.bid + stops), base)
        first = round(first, spec_now.digits)
        # Jarak antar layer = offset layer pertama, dengan lantai 1 pip.
        pip = spec_now.point * (10 if spec_now.digits in (3, 5) else 1)
        step = max(off, pip)
        placed: list[tuple[int, float, int]] = []
        for k in range(layers):
            price_k = first - k * step if side == "BUY" else first + k * step
            price_k = round(price_k, spec_now.digits)
            if price_k <= 0:
                break
            try:
                resp = self._link.order_send(
                    side=side, symbol=plan.symbol, lots=plan.lots, sl=0.0, tp=0.0,
                    magic=self._cfg.magic, deviation=self._cfg.deviation_points,
                    comment=f"xau-monitor {sig.mode or '-'} {sig.kind} L{k + 1}",
                    price=price_k, otype="limit",
                )
            except Mt4Rejected as exc:
                self._audit({"event": "ea_reject", "side": side, "layer": k + 1,
                             "code": exc.code, "detail": str(exc),
                             "plan": _plan_dict(plan)})
                if not placed:
                    return self._msg("🛑", "DITOLAK BROKER/EA",
                                     f"layer {k + 1}: {exc.code} "
                                     f"{exc.params.get('msg', '')}")
                break
            except Mt4Error as exc:
                self._audit({"event": "ea_unavailable", "side": side, "layer": k + 1,
                             "detail": str(exc), "plan": _plan_dict(plan)})
                if not placed:
                    return self._msg("📡", "BRIDGE MT4 GAGAL", str(exc))
                break
            try:
                ticket = int(resp.get("ticket", 0) or 0)
            except (TypeError, ValueError):
                ticket = 0
            if ticket > 0:
                self._trailer.track(ticket=ticket, side=side, lots=plan.lots,
                                    price=price_k, mode=sig.mode or "")
            placed.append((k + 1, price_k, ticket))

        if not placed:
            return self._msg("🛑", "ORDER GAGAL", "tidak ada layer yang terpasang")
        self._audit({"event": "pending", "side": side, "magic": self._cfg.magic,
                     "layers": len(placed),
                     "tickets": [t for _, _, t in placed],
                     "limits": [p for _, p, _ in placed],
                     "advisor": self._advisor.name, "decision": decision.action,
                     "rationale": decision.rationale, "confidence": decision.confidence,
                     "plan": _plan_dict(plan)})
        if len(placed) == 1:
            _, price, ticket = placed[0]
            return self._limit_msg(sig, plan, {"ticket": ticket}, decision, side,
                                   spec_now.digits, price, ticket)
        return self._layers_msg(sig, plan, decision, side, spec_now.digits,
                                placed, layers, account)

    # ---- helpers ---------------------------------------------------
    def _total_positions(self) -> int:
        magics = (self._cfg.magic,) + tuple(self._cfg.sibling_magics)
        return sum(len(self._link.positions(self._cfg.symbol, m)) for m in set(magics))

    def _broker_snapshot(self) -> tuple[list, SymbolSpec, AccountState]:
        positions = self._link.positions(self._cfg.symbol, self._cfg.magic)
        spec = SymbolSpec.from_wire(self._link.symbol_info(self._cfg.symbol))
        account = AccountState.from_wire(self._link.ping())
        # Jejak akun di log: tanpa ini, guard_reject `margin` sulit didiagnosa.
        log.info("akun broker: equity=%.2f balance=%.2f free=%.2f leverage=%d "
                 "margin/lot=%.2f lot_min=%.2f", account.equity, account.balance,
                 account.free_margin, account.leverage, spec.margin_per_lot,
                 spec.lot_min)
        return positions, spec, account

    def _dedupe_key(self, sig: Signal, side: str) -> str:
        bucket = int(sig.ts // max(self._cfg.dedupe_seconds, 1))
        # 8 desimal = batas atas digits broker (lihat strategy.Engine); zona
        # sudah dibulatkan engine ke digits, jadi 2 desimal akan menyatukan
        # level FX berbeda (1.08500 vs 1.08543 -> "1.09") dan salah dedupe.
        return f"{side}:{sig.zone:.8f}:{bucket}"

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        self._recent = {k: v for k, v in self._recent.items()
                        if now - v < self._cfg.dedupe_seconds}
        return key in self._recent

    def _audit(self, record: dict) -> None:
        row = {"ts": round(time.time(), 3), "symbol": self._cfg.symbol, **record}
        try:
            parent = os.path.dirname(self._cfg.log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self._cfg.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("audit log gagal ditulis (%s): %s", self._cfg.log_path, exc)

    def _limit_msg(self, sig: Signal, plan, resp: dict, decision, side: str,
                   digits: int, limit_price: float, ticket: int) -> str:
        d = max(0, int(digits))
        conf = f" · conf {decision.confidence:.2f}" if decision.confidence else ""
        reason = html.escape(decision.rationale or "-")
        mins = self._cfg.pending_ttl_s / 60.0
        tix = f"#{ticket}" if ticket > 0 else html.escape(str(resp.get("ticket", "?")))
        req = getattr(plan, "lots_requested", None)
        lot_note = ("" if not req or abs(req - plan.lots) < 1e-9
                    else f" (AI minta {req:g}, dikunci guard)")
        return (
            f"📌 <b>{html.escape(sig.kind)} — LIMIT DIPASANG</b>\n"
            f"{side} <b>{plan.lots:g}</b> lot{lot_note} @ {limit_price:.{d}f}\n"
            f"SL/TP broker: kosong · berlaku {mins:g} menit · ticket <b>{tix}</b>\n"
            f"Basis {plan.basis:+.{d}f} · TP trailing {self._cfg.trail_retrace:.0%} "
            f"dari puncak\n"
            f"🤖 {html.escape(self._advisor.name)}{conf}: {reason}\n"
            f"⏱ {time.strftime('%d-%m %H:%M')}"
        )

    def _layers_msg(self, sig: Signal, plan, decision, side: str, digits: int,
                    placed: list[tuple[int, float, int]], wanted: int,
                    account) -> str:
        d = max(0, int(digits))
        conf = f" · conf {decision.confidence:.2f}" if decision.confidence else ""
        reason = html.escape(decision.rationale or "-")
        mins = self._cfg.pending_ttl_s / 60.0
        lines = [
            f"📌 <b>{html.escape(sig.kind)} — {len(placed)} LIMIT (layering)</b>",
            f"{side} <b>{plan.lots:g}</b> lot/layer · total "
            f"<b>{plan.lots * len(placed):g}</b> lot",
            f"SL/TP broker: kosong · berlaku {mins:g} menit · free margin "
            f"{account.free_margin:.2f}",
        ]
        for k, price, ticket in placed:
            tix = f"#{ticket}" if ticket > 0 else "?"
            lines.append(f"  L{k}: {price:.{d}f} → ticket {tix}")
        if len(placed) < wanted:
            lines.append(f"⚠️ hanya {len(placed)}/{wanted} layer terpasang")
        lines.append(f"🤖 {html.escape(self._advisor.name)}{conf}: {reason}")
        lines.append(f"⏱ {time.strftime('%d-%m %H:%M')}")
        return "\n".join(lines)

    def _msg(self, icon: str, title: str, detail: str) -> str:
        return (f"{icon} <b>{html.escape(self._cfg.symbol)} — {html.escape(title)}</b>\n"
                f"{html.escape(detail)}\n⏱ {time.strftime('%d-%m %H:%M')}")


def snapshot_metrics(snaps: dict, price: float, zone: float,
                     digits: int = 2) -> tuple[tuple[str, float], ...]:
    """Angka mentah untuk AI: supaya ia bisa membandingkan dengan kalibrasi engine,
    bukan menebak dari kalimat."""
    out: dict[str, float] = {}
    for tf, snap in snaps.items():
        if snap is not None:
            out[f"pos20_{tf}"] = round(snap.pos20, 1)
    m5, m15 = snaps.get("M5"), snaps.get("M15")
    m1 = snaps.get("M1")
    atr_dp = max(3, int(digits) + 1)  # FX 5-digit butuh 6 desimal, bukan 3
    out["atr_m1"] = round(m1.atr, atr_dp) if m1 else 0.0
    out["atr_m5"] = round(m5.atr, atr_dp) if m5 else 0.0
    out["atr_m15"] = round(m15.atr, atr_dp) if m15 else 0.0
    if m5 and m5.bars:
        b = m5.bars[-1]
        rng = (b.h - b.l) or 1e-9
        out["m5_wick_up_pct"] = round((b.h - max(b.o, b.c)) / rng * 100, 1)
        out["m5_wick_dn_pct"] = round((min(b.o, b.c) - b.l) / rng * 100, 1)
        out["m5_body_pct"] = round(abs(b.c - b.o) / rng * 100, 1)
        out["m5_thrust_threshold"] = round(1.2 * m5.atr, atr_dp)
    if m15 and m15.atr and zone:
        out["zone_dist_atr_m15"] = round(abs(price - zone) / m15.atr, 2)
    return tuple(sorted(out.items()))


def _plan_dict(plan) -> dict:
    return {
        "side": plan.side, "lots": plan.lots, "entry": plan.entry,
        "stop": plan.stop, "tp": plan.take_profit, "basis": plan.basis,
        "risk_amount": plan.risk_amount, "risk_points": plan.risk_points,
        "lots_requested": plan.lots_requested,
    }
