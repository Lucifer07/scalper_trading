"""Jalur order: siapkan executor per mode, submit sinyal, drain hasil.

Executor tetap sinkron; LLM advisor bisa 10-30s sehingga eksekusi berjalan di
satu worker thread dan hasilnya dikirim balik lewat queue ke loop monitor.
"""
from __future__ import annotations

import logging
import queue
from concurrent.futures import Future, ThreadPoolExecutor

from .advisor import build_advisor
from .config import OrderSettings, Settings
from .executor import ExecConfig, OrderExecutor
from .mt4_link import Mt4Link
from .orders import RiskConfig

log = logging.getLogger("orders")


class OrderFlow:
    def __init__(self, settings: Settings, link: Mt4Link) -> None:
        self.settings = settings
        self.link = link
        self.executors: dict[str, OrderExecutor] = {}
        self.layering = False
        self.state = "OFF"
        self._pool: ThreadPoolExecutor | None = None
        self._resultq: "queue.Queue[str]" = queue.Queue()
        self._last: Future | None = None

    @property
    def active(self) -> bool:
        return bool(self.executors)

    def has_position(self, mode: str) -> bool:
        """Apakah mode ini punya posisi nyata (terisi & belum ditutup trailing)?

        Mode yang tidak dikenal -> False: lebih baik bungkam alert daripada
        kirim instruksi 'close manual' untuk posisi yang tidak ada.
        """
        executor = self.executors.get(mode)
        return executor is not None and executor.has_position()

    # ---- persiapan -------------------------------------------------
    def build(self) -> None:
        o = self.settings.orders
        magics = {mode: o.magic_for(mode) for mode in self.settings.modes}
        for mode in self.settings.modes:
            siblings = tuple(m for k, m in magics.items() if k != mode)
            self.executors[mode] = self._executor(o, mode, siblings)
        self.layering = o.layering_enabled
        self.state = (f"ARMED {o.symbol} · {' + '.join(self.settings.modes)} · "
                      f"maks {o.max_positions} posisi"
                      + (" · layering ON" if o.layering_enabled else ""))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="order")

    def _executor(self, o: OrderSettings, mode: str,
                  siblings: tuple[int, ...]) -> OrderExecutor:
        advisor = build_advisor(o.advisor_url, o.advisor_model, o.advisor_api_key,
                                o.advisor_timeout, o.rules_md, o.rules_md_max_chars,
                                o.band_atr, o.advisor_cli_model, o.advisor_bin,
                                o.advisor_agent, o.broker_protection, o.symbol,
                                o.layering_min if o.layering_enabled else None,
                                o.advisor_retries)
        risk = RiskConfig(o.risk_pct, o.max_lot, o.max_spread_points, o.max_basis,
                          o.attach_tp, o.attach_sl)
        cfg = ExecConfig(o.symbol, o.magic_for(mode), o.deviation_points,
                         o.dedupe_seconds, o.band_atr, o.log_path, o.max_drift,
                         siblings, o.max_positions, o.pending_ttl_s,
                         o.trail_retrace_pct / 100.0,
                         layering_enabled=o.layering_enabled,
                         layering_min=o.layering_min,
                         layering_margin_safety=o.layering_margin_safety,
                         layering_max_margin_pct=o.layering_max_margin_pct)
        if not o.broker_protection:
            log.warning("ORDER TANPA SL/TP di broker (ATTACH_SL=0, ATTACH_TP=0): "
                        "exit manual; tidak ada proteksi bila monitor/koneksi mati")
        log.warning("ORDER AKTIF [%s]: %s magic=%d via EA :%d · advisor=%s · "
                    "max lot %s · maks %d posisi", mode, o.symbol, cfg.magic,
                    o.port, advisor.name, o.max_lot, o.max_positions)
        return OrderExecutor(self.link, advisor, risk, cfg)

    # ---- runtime ---------------------------------------------------
    def submit(self, sig, atr: float, metrics) -> None:
        if self._last is not None and not self._last.done():
            log.warning("sinyal %s/%s diabaikan: eksekusi sebelumnya masih berjalan",
                        sig.mode, sig.kind)
            return
        if self._pool is None:
            log.error("sinyal %s/%s tidak dieksekusi: order pool tidak siap",
                      sig.mode, sig.kind)
            return
        self._last = self._pool.submit(self._run, sig, atr, metrics)

    def _run(self, sig, atr: float, metrics) -> None:
        try:
            executor = self.executors.get(sig.mode) or next(iter(self.executors.values()))
            self._resultq.put(executor.execute(sig, atr, metrics))
        except Exception:  # noqa: BLE001 — worker tidak boleh mati diam-diam
            log.exception("worker order gagal")
            self._resultq.put("⚠️ <b>ORDER WORKER ERROR</b> — lihat journalctl")

    def set_layering(self, enabled: bool) -> tuple[bool, str]:
        """Toggle layering runtime (panel). Berlaku sampai proses restart."""
        self.layering = bool(enabled)
        for executor in self.executors.values():
            executor.layering = self.layering
        log.warning("layering %s (runtime, dari panel)", "ON" if self.layering else "OFF")
        return True, f"layering {'ON' if self.layering else 'OFF'} (runtime)"

    def poll(self) -> list[str]:
        """Pending expiry/fill + trailing TP dari semua executor."""
        out: list[str] = []
        for executor in self.executors.values():
            out.extend(executor.poll())
        return out

    def drain(self) -> list[str]:
        out: list[str] = []
        while True:
            try:
                out.append(self._resultq.get_nowait())
            except queue.Empty:
                return out

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


def position_alert_deliverable(sig, orders: "OrderFlow | None") -> bool:
    """Gerbang alert manajemen posisi (TP*/INVALIDATED) sebelum dikirim ke user.

    Kenapa: engine menyimpan rencana entry sebagai 'paper position' walau jalur
    order gagal di tengah jalan (advisor unavailable/timeout, guard reject,
    EA down). Tanpa gerbang ini, 'TP2 tercapai — close manual' terkirim untuk
    posisi yang tidak pernah ada (kejadian EURUSD 19-11-2026 20:23).

    Aturan:
    - Mode manual (orders None / belum armed) -> selalu True: alert level TP
      memang fitur untuk trader manual.
    - Jalur order armed: TP*/INVALIDATED hanya boleh keluar bila mode itu
      punya posisi nyata di broker.
    - Sinyal lain (ENTRY_*, HOLD) tidak pernah digerbang — bukan ranahnya.
    """
    if orders is None or not orders.active:
        return True
    if sig.kind.startswith("TP") or sig.kind == "INVALIDATED":
        return orders.has_position(sig.mode)
    return True
