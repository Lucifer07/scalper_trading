"""Runtime monitor: feed, tick router, loop, control API, shutdown.

Dipisah dari `main` supaya entrypoint tetap tipis dan status/report bisa
dipanggil dari control API (panel GUI) tanpa menyentuh argumen CLI. Detail
order ada di `order_flow`, laporan di `telemetry`, watchdog di `alerts`.
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time

from . import report, readiness, telemetry
from .advisor import AdvisorUnavailable
from .alerts import FeedWatch
from .candles import M1Builder
from .commands import CommandListener
from .config import Settings
from .control import ControlServer
from .executor import snapshot_metrics
from .feed_mt4 import SeedError
from .feed_mt4_ticks import Mt4BarTicks
from .loss_guard import LossGuard
from .mt4_link import Mt4Error, Mt4Link
from .order_flow import OrderFlow, position_alert_deliverable
from .orders import GuardRejection, SymbolSpec
from .pipeline import (FIRST_TICK_TIMEOUT_S, atr_m1, build_snaps, deliver,
                       forming_bar, market_dead, wait_first_tick)
from .strategy import Engine
from .symbol_switch import SymbolSwitch
from .telegram import TelegramNotifier
from .wiring import (FeedConfigError, open_link, seed_history, start_control,
                     start_feeds)

log = logging.getLogger("main")


def _fmt_free(value: float) -> str:
    """Desimal sesuai harga itu sendiri — dipakai saat digits broker belum
    terbaca (log tick pertama): FX 1.08543 tampil utuh, emas 3872.27 utuh."""
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


class MonitorRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.notifier: TelegramNotifier | None = None
        self.link: Mt4Link | None = None
        self.orders: OrderFlow | None = None
        self.broker_ticks: Mt4BarTicks | None = None
        self.control: ControlServer | None = None
        self.watch: FeedWatch | None = None
        self.guard: LossGuard | None = None
        self.outq: "queue.Queue[tuple[str, int, float]]" = queue.Queue()
        self._stop = threading.Event()
        self.builder = M1Builder()
        self.engines: dict[str, Engine] = {}
        self.hist: dict = {}
        self.digits = 2
        self.price = 0.0
        self.snaps: dict = {}
        self.forming = None
        self.started_at = 0.0
        self._last_seed_refresh = 0.0
        self._market_dead_prev = False
        self.switcher = SymbolSwitch(self)

    # ---- persiapan -------------------------------------------------
    def prepare(self) -> int | None:
        s = self.settings
        if not s.dry_run:
            self.notifier = TelegramNotifier(s.telegram_token, s.telegram_chat_id)
            bot = self.notifier.health()
            if not bot:
                log.error("Telegram getMe gagal — cek TELEGRAM_BOT_TOKEN "
                          "(jalankan ulang setelah benar)")
                return 2
            log.info("Telegram bot @%s OK", bot)
        else:
            log.warning("DRY-RUN: TELEGRAM_BOT_TOKEN/CHAT_ID belum diisi — "
                        "sinyal hanya tampil di console")

        # Bridge MT4 selalu dibutuhkan: tick (dan history broker) hanya dari sini.
        try:
            self.link = open_link(s)
        except OSError as exc:
            log.error("port bridge %d tidak bisa dipakai: %s — tick hanya dari MT4",
                      s.orders.port, exc)
            return 2
        self.guard = self._make_guard()
        if s.orders.enabled:
            if self.guard is not None and self.guard.locked:
                log.error("jalur order TIDAK di-arm: hard stop sudah pernah aktif "
                          "(%s)", s.risk_guard.latch_path)
            else:
                try:
                    self.orders = OrderFlow(s, self.link)
                    self.orders.build()
                except AdvisorUnavailable as exc:
                    log.error("jalur order gagal disiapkan: %s", exc)
                    return 2
        return None

    def _make_guard(self) -> LossGuard | None:
        """Hard stop opt-in: LOSS_LIMIT_USD > 0. None = guard mati."""
        cfg = self.settings.risk_guard
        if cfg.limit_usd <= 0 or self.link is None:
            return None
        return LossGuard(cfg, self.link, self.notifier,
                         symbol=self.settings.orders.symbol,
                         magic=self.settings.orders.magic)

    def _announce_lock(self) -> None:
        cfg = self.settings.risk_guard
        msg = (f"🔒 TRADING LOCKED — hard stop -${cfg.limit_usd:.2f} pernah aktif.\n"
               f"Monitor {self.settings.symbol} TIDAK dijalankan; tidak ada order.\n"
               f"Buka manual: hapus {cfg.latch_path} dan {cfg.state_path}.")
        log.error("%s", msg.replace("\n", " | "))
        deliver(self.notifier, msg)

    def _start_feeds(self) -> int | None:
        try:
            self.broker_ticks = start_feeds(
                self.settings, self.link, self.outq)
        except FeedConfigError as exc:
            log.error("%s", exc)
            return 2
        return None

    def _start_control(self) -> None:
        self.control = start_control(self.settings, self.status_payload,
                                     self.report_text,
                                     self.switcher.set_symbol, self.set_layering)

    def set_layering(self, enabled: bool) -> tuple[bool, str]:
        """Dipanggil control API: on/off layering saat runtime (tanpa restart)."""
        if self.orders is None:
            return False, "jalur order belum siap — layering tidak bisa diubah"
        return self.orders.set_layering(enabled)

    # ---- status & laporan ------------------------------------------
    def status_payload(self) -> dict:
        s = self.settings
        return telemetry.status_payload(
            s, price=self.price, started_at=self.started_at,
            broker=self.broker_ticks, engines=self.engines,
            link=self.link, order_state=self.orders.state if self.orders else "OFF",
            layering=self.orders.layering if self.orders else False,
            digits=self.digits,
            readiness=telemetry.readiness_payload(
                engines=self.engines, snaps=self.snaps, hist=self.hist,
                price=self.price, forming=self.forming, digits=self.digits))

    def report_text(self, kind: str) -> str:
        return self.handle_command(kind)

    def handle_command(self, cmd: str) -> str:
        return telemetry.command_text(
            self.settings, cmd, price=self.price, snaps=self.snaps, hist=self.hist,
            engines=self.engines, forming=self.forming, broker=self.broker_ticks,
            started_at=self.started_at)

    def _broker_digits(self) -> int:
        """Presisi harga dari broker — gagal = berhenti, bukan asumsi 2 desimal.

        Tanpa digits yang benar, level FX (1.32105) dibulatkan ke 1.32 dan stop
        menjadi ribuan pip; lebih baik monitor mati daripada mengirim order salah.
        """
        if self.link is None:
            raise RuntimeError("bridge MT4 tidak ada; digits broker tidak bisa dibaca")
        try:
            spec = SymbolSpec.from_wire(
                self.link.symbol_info(self.settings.orders.symbol))
        except (Mt4Error, GuardRejection, ValueError) as exc:
            raise RuntimeError(
                f"symbol_info {self.settings.orders.symbol} gagal: {exc}") from None
        return spec.digits

    # ---- history ---------------------------------------------------
    # (seed_history ada di wiring.py; dipakai bersama symbol_switch)

    # ---- loop ------------------------------------------------------
    def run(self) -> int:
        code = self.prepare()
        if code is not None:
            return code
        if self.guard is not None and self.guard.locked:
            self._announce_lock()
            self._shutdown()
            return 0
        code = self._start_feeds()
        if code is not None:
            self._shutdown()
            return code
        self._start_control()
        self._install_signals()
        self.started_at = time.time()

        log.info("menunggu tick pertama %s (max %ds)...", self.settings.symbol,
                 FIRST_TICK_TIMEOUT_S)
        first_mid = wait_first_tick(self.outq, FIRST_TICK_TIMEOUT_S)
        self.price = first_mid or 0.0
        if first_mid is None:
            log.error("tidak ada tick MT4 — cek EA bridge (terminal jalan? "
                      "simbol %s ada di Market Watch?)", self.settings.orders.symbol)
        else:
            log.info("tick pertama MT4: %s", _fmt_free(first_mid))

        try:
            self.hist = seed_history(self.settings, self.link, first_mid)
        except SeedError as exc:
            log.error("HISTORY_SOURCE=mt4 tapi broker tidak memberi history: %s", exc)
            self._shutdown()
            return 2

        s = self.settings
        try:
            self.digits = self._broker_digits()
        except RuntimeError as exc:
            log.error("tidak bisa menentukan digits broker — monitor berhenti: %s", exc)
            self._shutdown()
            return 2
        log.warning("presisi broker %s: digits=%d (point=%s)",
                    s.symbol, self.digits, 10.0 ** -self.digits)
        self.engines = {mode: Engine(s.cooldown_minutes, mode, digits=self.digits,
                                     limit_offset_pct=s.orders.limit_offset_pct,
                                     limit_offset_max_pips=s.orders.limit_offset_max_pips)
                        for mode in s.modes}
        self.watch = FeedWatch(self.notifier, s)
        deliver(self.notifier, telemetry.announce_text(
            s, hist_len=len(self.hist),
            live=self.notifier is not None,
            order_state=self.orders.state if self.orders else "OFF"))
        if self.notifier and s.commands_enabled:
            CommandListener(s.telegram_token, s.telegram_chat_id,
                            self.notifier, self.handle_command).start()
        elif self.notifier:
            log.info("listener perintah dimatikan (COMMANDS_ENABLED=0) — "
                     "bot token dipakai bersama antar instance")

        self._last_seed_refresh = time.time()
        try:
            while not self._stop.is_set():
                self._tick()
        finally:
            self._shutdown()
        return 0

    def _tick(self) -> None:
        locked = False
        if self.guard is not None:
            if self.guard.check():
                log.error("HARD STOP aktif — monitor berhenti, tidak ada order baru")
                self._stop.set()
                return
            locked = self.guard.locked
        if not locked:                      # latch aktif = jangan sentuh order apa pun
            self.switcher.drain()
            if self.orders is not None:
                for text in self.orders.poll():  # pending expiry/fill + trailing TP
                    deliver(self.notifier, text)
                for text in self.orders.drain():
                    deliver(self.notifier, text)

        m1_closed = False
        try:
            while True:  # drain batch
                _, ts_ms, mid = self.outq.get(timeout=1.0)
                self.price = mid
                if self.builder.add_tick(ts_ms, mid) is not None:
                    m1_closed = True
                if self.outq.empty():
                    break
        except queue.Empty:
            pass

        if self.watch is not None:
            self.watch.check(self.broker_ticks, self.started_at)
        if m1_closed and self.price:
            self._on_m1_close()
        self._maybe_refresh_seed()
        if self.watch is not None:
            self.watch.heartbeat(price=self.price, engines=self.engines,
                                 broker=self.broker_ticks,
                                 started_at=self.started_at)

    def _on_m1_close(self) -> None:
        s = self.settings
        if self.guard is not None and self.guard.locked:
            log.info("M1 close %s | hard stop aktif — evaluasi sinyal dimatikan",
                     f"{self.price:.{self.digits}f}")
            return
        if s.dead_market_guard:
            dead = market_dead(self.builder.closed(), self.price,
                               s.dead_market_flat_bars)
            if dead != self._market_dead_prev:
                self._market_dead_prev = dead
                log.warning("market %s — evaluasi sinyal %s",
                            "BEKU" if dead else "bergerak lagi",
                            "dijeda" if dead else "dilanjutkan")
                deliver(self.notifier, report.fmt_market_guard(
                    s.symbol, dead, s.dead_market_flat_bars, self.price,
                    digits=self.digits))
            if dead:
                # Feed beku (market tutup): jangan evaluasi — window 20-bar
                # bergeser sendiri dan menghasilkan HOLD/entry palsu.
                log.info("M1 close %s | market BEKU — evaluasi dijeda",
                         f"{self.price:.{self.digits}f}")
                return
        snaps = build_snaps(self.hist, self.builder, self.price)
        forming = forming_bar(self.builder.closed(), self.price, time.time())
        self.snaps, self.forming = snaps, forming
        uptime = max(time.time() - self.started_at, 1.0)
        rate = self.broker_ticks.tick_count / uptime if self.broker_ticks else 0.0
        for mode, eng in self.engines.items():
            sig = eng.step(snaps, self.price, now=time.time(), forming_m15=forming)
            if sig:
                if not position_alert_deliverable(sig, self.orders):
                    log.warning("alert %s mode %s DIBUNGKAM: tidak ada posisi "
                                "terbuka (order tidak pernah tereksekusi) — "
                                "rencana TP engine tanpa posisi nyata, lihat audit",
                                sig.kind, mode)
                else:
                    deliver(self.notifier, report.fmt_signal(
                        sig, self.settings.symbol, rate, digits=self.digits))
                if self.orders is not None and sig.kind.startswith("ENTRY_"):
                    self.orders.submit(sig, atr_m1(snaps),
                                       snapshot_metrics(snaps, sig.price, sig.zone,
                                                        self.digits))
        log.info("M1 close %s | %s", f"{self.price:.{self.digits}f}",
                 " · ".join(f"{m}={e.state.value}({e.blocked_reason or '-'})"
                            for m, e in self.engines.items()))

    def _maybe_refresh_seed(self) -> None:
        if time.time() - self._last_seed_refresh <= 3600:
            return
        self._last_seed_refresh = time.time()
        try:
            refreshed = seed_history(self.settings, self.link, self.price)
        except SeedError as exc:
            log.warning("refresh history gagal: %s", exc)
            return
        if refreshed:
            self.hist = refreshed
            log.info("seed TF besar di-refresh (%s)", self.settings.history_source)

    # ---- lifecycle -------------------------------------------------
    def _install_signals(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._on_signal)
            signal.signal(signal.SIGTERM, self._on_signal)
        except ValueError:
            log.warning("signal handler tidak bisa dipasang (bukan main thread)")

    def _on_signal(self, _sig, _frame) -> None:
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        self._stop.set()
        if self.control is not None:
            self.control.stop()
            self.control = None
        if self.broker_ticks is not None:
            self.broker_ticks.stop()
        if self.orders is not None:
            self.orders.shutdown()
        if self.link is not None:
            self.link.shutdown()
            self.link = None
        log.info("monitor berhenti (total ticks MT4=%d)",
                 self.broker_ticks.tick_count if self.broker_ticks else 0)
