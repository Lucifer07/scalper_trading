"""Environment-driven settings. No hardcoded secrets — see .env.example.

Pattern: single immutable Settings dataclass assembled by load_settings().
Fails loudly (SystemExit) on contradictory config instead of silent defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Re-export untuk kompatibilitas import lama (order_flow, wiring, tests).
from .config_env import _die, _flag, _float, _int, load_dotenv  # noqa: F401
from .config_orders import OrderSettings, _order_settings  # noqa: F401


@dataclass(frozen=True)
class PanelSettings:
    """Control API + log file untuk panel GUI. Loopback-only (tanpa auth)."""

    control_enabled: bool
    control_host: str
    control_port: int
    log_file: str
    log_max_bytes: int
    log_backups: int


@dataclass(frozen=True)
class RiskGuardSettings:
    """Hard loss stop: rugi akun >= limit_usd -> flatten + matikan app.

    `limit_usd == 0` = guard mati (perilaku lama). Saat aktif, daftar unit
    systemd wajib diisi eksplisit — jangan menebak nama service deployment.
    """

    limit_usd: float
    poll_s: float
    confirm: int
    state_path: str
    latch_path: str
    lock_path: str
    symbols_path: str
    stop_units: tuple[str, ...]
    systemctl: str


@dataclass(frozen=True)
class Settings:
    symbol: str
    telegram_token: str
    telegram_chat_id: str
    cooldown_minutes: int
    yahoo_context: bool
    yahoo_symbol: str
    heartbeat_minutes: int
    mode: str
    modes: tuple[str, ...]
    commands_enabled: bool
    history_source: str
    tick_poll_ms: int
    dead_market_guard: bool
    dead_market_flat_bars: int
    orders: OrderSettings
    panel: PanelSettings
    risk_guard: RiskGuardSettings

    @property
    def dry_run(self) -> bool:
        """True = no Telegram credentials; alerts print to console instead."""
        return not (self.telegram_token and self.telegram_chat_id)


# Var feed kantor (FIX worker) yang sudah dipensiunkan. Tick kini HANYA dari
# MT4 (EA bridge) — keberadaan var ini berarti .env basi dan harus dibersihkan,
# bukan diam-diam diabaikan.
_LEGACY_WORKER_ENV = ("WORKER_GRPC_ADDR", "TICK_SOURCE", "SESSION_ID",
                      "MARKET_DEPTH", "BROKER_TICKS", "FALLBACK_AFTER_S")


def _reject_legacy_worker_env() -> None:
    stale = [k for k in _LEGACY_WORKER_ENV if os.environ.get(k, "").strip()]
    if stale:
        _die(f"var legacy feed kantor masih ada: {', '.join(stale)} — "
             "tick sekarang hanya dari MT4 (EA bridge); hapus var itu dari .env")


def load_settings(env_file: str = ".env") -> Settings:
    load_dotenv(env_file)
    _reject_legacy_worker_env()

    symbol = os.environ.get("SYMBOL", "XAUUSD").strip().upper()
    if not symbol:
        _die("SYMBOL kosong — set SYMBOL=XAUUSD di .env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and not chat:
        _die("TELEGRAM_BOT_TOKEN terisi tapi TELEGRAM_CHAT_ID kosong — tidak boleh setengah konfigurasi")

    try:
        cooldown = int(os.environ.get("COOLDOWN_MINUTES", "45"))
        heartbeat = int(os.environ.get("HEARTBEAT_MINUTES", "0"))
    except ValueError as e:
        _die(f"Setting numerik tidak valid: {e}")

    mode = os.environ.get("MODE", "sniper").strip().lower()
    if mode not in ("sniper", "handgun"):
        _die(f"MODE harus 'sniper' atau 'handgun', bukan {mode!r}")
    raw_modes = os.environ.get("MODES", "").strip().lower()
    modes = tuple(m.strip() for m in raw_modes.split(",") if m.strip()) or (mode,)
    for m in modes:
        if m not in ("sniper", "handgun"):
            _die(f"MODES berisi nilai tidak dikenal: {m!r}")
    if len(set(modes)) != len(modes):
        _die(f"MODES duplikat: {modes}")

    tick_poll_ms = _int("MT4_TICK_POLL_MS", 500)
    if tick_poll_ms < 100:
        _die(f"MT4_TICK_POLL_MS minimal 100, bukan {tick_poll_ms}")

    history_source = os.environ.get("HISTORY_SOURCE", "auto").strip().lower()
    if history_source not in ("auto", "mt4", "yahoo"):
        _die(f"HISTORY_SOURCE harus auto/mt4/yahoo, bukan {history_source!r}")

    dead_guard = _flag("DEAD_MARKET_GUARD", True)
    flat_bars = _int("DEAD_MARKET_FLAT_BARS", 5)
    if flat_bars < 2:
        _die(f"DEAD_MARKET_FLAT_BARS minimal 2 (1 bar doji akan false-positive), "
             f"bukan {flat_bars}")

    orders = _order_settings(symbol)
    panel = _panel_settings()
    if panel.control_port == orders.port:
        _die(f"CONTROL_PORT dan MT4_BRIDGE_PORT sama ({orders.port}) — port harus beda")

    return Settings(
        symbol=symbol,
        telegram_token=token,
        telegram_chat_id=chat,
        cooldown_minutes=cooldown,
        yahoo_context=os.environ.get("YAHOO_CONTEXT", "1").strip() == "1",
        yahoo_symbol=os.environ.get("YAHOO_SYMBOL", "GC=F").strip(),
        heartbeat_minutes=heartbeat,
        mode=mode,
        modes=modes,
        commands_enabled=os.environ.get("COMMANDS_ENABLED", "1").strip() == "1",
        history_source=history_source,
        tick_poll_ms=tick_poll_ms,
        dead_market_guard=dead_guard,
        dead_market_flat_bars=flat_bars,
        orders=orders,
        panel=panel,
        risk_guard=_risk_guard_settings(),
    )


def _panel_settings() -> PanelSettings:
    """Panel GUI: control API + log file. Tanpa auth => wajib loopback."""
    host = os.environ.get("CONTROL_HOST", "127.0.0.1").strip()
    port = _int("CONTROL_PORT", 8772)
    enabled = _flag("CONTROL_ENABLED", True)
    if not 1 <= port <= 65535:
        _die(f"CONTROL_PORT tidak valid: {port}")
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if enabled and not loopback:
        _die(f"CONTROL_HOST={host} membuka control API tanpa autentikasi — "
             "pakai 127.0.0.1 dan akses panel via SSH tunnel")
    max_bytes = _int("LOG_MAX_BYTES", 2_000_000)
    backups = _int("LOG_BACKUPS", 5)
    if max_bytes < 100_000:
        _die(f"LOG_MAX_BYTES minimal 100000, bukan {max_bytes}")
    if backups < 0:
        _die(f"LOG_BACKUPS tidak boleh negatif: {backups}")
    return PanelSettings(
        control_enabled=enabled,
        control_host=host,
        control_port=port,
        log_file=os.environ.get("LOG_FILE", "logs/monitor.log").strip(),
        log_max_bytes=max_bytes,
        log_backups=backups,
    )


def _risk_guard_settings() -> RiskGuardSettings:
    """Hard loss stop. Aktif hanya bila LOSS_LIMIT_USD > 0 (opt-in eksplisit)."""
    limit = _float("LOSS_LIMIT_USD", 0.0)
    if limit < 0:
        _die(f"LOSS_LIMIT_USD tidak boleh negatif: {limit}")
    poll = _float("LOSS_GUARD_POLL_S", 5.0)
    if not 1.0 <= poll <= 300.0:
        _die(f"LOSS_GUARD_POLL_S harus 1..300 detik, bukan {poll}")
    confirm = _int("LOSS_GUARD_CONFIRM", 2)
    if not 1 <= confirm <= 10:
        _die(f"LOSS_GUARD_CONFIRM harus 1..10 sampel, bukan {confirm}")
    raw_units = os.environ.get("LOSS_GUARD_STOP_UNITS", "").strip()
    stop_units = tuple(u.strip() for u in raw_units.split(",") if u.strip())
    if limit > 0 and not stop_units:
        _die("LOSS_LIMIT_USD aktif tapi LOSS_GUARD_STOP_UNITS kosong — "
             "tanpa itu hanya proses ini yang berhenti, instance lain tetap trading")
    return RiskGuardSettings(
        limit_usd=limit,
        poll_s=poll,
        confirm=confirm,
        state_path=os.environ.get("LOSS_GUARD_STATE", "state/loss_guard.json").strip(),
        latch_path=os.environ.get("LOSS_GUARD_LATCH", "state/trading_locked.json").strip(),
        lock_path=os.environ.get("LOSS_GUARD_LOCK", "state/loss_guard.lock").strip(),
        symbols_path=os.environ.get("LOSS_GUARD_SYMBOLS", "symbols.json").strip(),
        stop_units=stop_units,
        systemctl=os.environ.get("LOSS_GUARD_SYSTEMCTL", "systemctl").strip(),
    )
