"""OrderSettings: seluruh jalur eksekusi MT4 + validasinya (dipisah dari `config`).

Semua OFF kecuali diaktifkan eksplisit dua lapis (ORDER_ENABLED + MT4_ALLOW_TRADING).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config_env import _die, _flag, _float, _int


@dataclass(frozen=True)
class OrderSettings:

    enabled: bool
    allow_trading: bool
    port: int
    bridge_host: str
    remote_allow: bool
    call_timeout: float
    symbol: str
    magic: int
    max_positions: int
    deviation_points: int
    risk_pct: float
    max_lot: float
    max_spread_points: int
    max_basis: float
    max_drift: float
    attach_sl: bool
    attach_tp: bool
    dedupe_seconds: int
    pending_ttl_s: float
    trail_retrace_pct: float
    limit_offset_pct: float
    limit_offset_max_pips: float
    layering_enabled: bool
    layering_min: int
    layering_margin_safety: float
    layering_max_margin_pct: float
    band_atr: float
    log_path: str
    advisor_modes: tuple[str, ...]
    advisor_url: str
    advisor_model: str
    advisor_api_key: str
    advisor_timeout: float
    advisor_cli_model: str
    advisor_bin: str
    advisor_agent: str
    advisor_retries: int
    rules_md: str
    rules_md_max_chars: int

    @property
    def armed(self) -> bool:
        """Dua lapis: config mengizinkan DAN allow_trading menyetujui."""
        return self.enabled and self.allow_trading

    def magic_for(self, mode: str) -> int:
        """Magic terpisah per mode: sniper=base, handgun=base+1."""
        offsets = {"sniper": 0, "handgun": 1}
        return self.magic + offsets.get(mode, 0)

    @property
    def broker_protection(self) -> bool:
        return self.attach_sl or self.attach_tp

    @property
    def advisor_on(self) -> bool:
        return bool(self.advisor_url or self.advisor_cli_model)


def _modes_env(name: str, running: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip().lower()
    picked = tuple(x.strip() for x in raw.split(",") if x.strip()) if raw else running
    unknown = [m for m in picked if m not in ("sniper", "handgun")]
    if unknown:
        _die(f"{name} berisi mode tidak dikenal: {unknown}")
    return picked


def _order_settings(feed_symbol: str, modes: tuple[str, ...] = ()) -> OrderSettings:
    orders = OrderSettings(
        enabled=_flag("ORDER_ENABLED", False),
        allow_trading=_flag("MT4_ALLOW_TRADING", False),
        port=_int("MT4_BRIDGE_PORT", 8770),
        bridge_host=os.environ.get("MT4_BRIDGE_HOST", "127.0.0.1").strip(),
        remote_allow=_flag("MT4_REMOTE_ALLOW", False),
        call_timeout=_float("MT4_CALL_TIMEOUT", 6.0),
        symbol=os.environ.get("MT4_SYMBOL", feed_symbol).strip().upper(),
        magic=_int("MT4_MAGIC", 20261116),
        max_positions=_int("MAX_POSITIONS", 2),
        deviation_points=_int("MT4_DEVIATION", 30),
        risk_pct=_float("RISK_PERCENT", 0.5),
        max_lot=_float("MAX_LOT", 0.10),
        max_spread_points=_int("MAX_SPREAD_POINTS", 50),
        max_basis=_float("MAX_BASIS", 3.0),
        max_drift=_float("MAX_DRIFT", 1.0),
        attach_sl=_flag("ATTACH_SL", True),
        attach_tp=_flag("ATTACH_TP", True),
        dedupe_seconds=_int("ORDER_DEDUPE_SECONDS", 300),
        pending_ttl_s=_float("PENDING_TTL_SECONDS", 300.0),
        trail_retrace_pct=_float("TRAIL_RETRACE_PCT", 20.0),
        limit_offset_pct=_float("LIMIT_OFFSET_PCT", 2.0),
        limit_offset_max_pips=_float("LIMIT_OFFSET_MAX_PIPS", 10.0),
        layering_enabled=_flag("LAYERING_ENABLED", False),
        layering_min=_int("LAYERING_MIN", 2),
        layering_margin_safety=_float("LAYERING_MARGIN_SAFETY", 0.9),
        layering_max_margin_pct=_float("LAYERING_MAX_MARGIN_PCT", 2.0),
        band_atr=_float("ADVISOR_BAND_ATR", 0.5),
        log_path=os.environ.get("ORDER_LOG", "orders.jsonl").strip(),
        advisor_modes=_modes_env("ADVISOR_MODES", modes),
        advisor_url=os.environ.get("ADVISOR_URL", "").strip(),
        advisor_model=os.environ.get("ADVISOR_MODEL", "").strip(),
        advisor_api_key=os.environ.get("ADVISOR_API_KEY", "").strip(),
        advisor_timeout=_float("ADVISOR_TIMEOUT", 30.0),
        advisor_cli_model=os.environ.get("ADVISOR_CLI_MODEL", "").strip(),
        advisor_bin=os.environ.get("ADVISOR_BIN", "opencode").strip(),
        advisor_agent=os.environ.get("ADVISOR_AGENT", "").strip(),
        advisor_retries=_int("ADVISOR_CLI_RETRIES", 1),
        rules_md=os.environ.get("RULES_MD", "~/ringkasan-scalping-xau-coach-tom.md").strip(),
        rules_md_max_chars=_int("RULES_MD_MAX_CHARS", 1500),
    )
    _validate_orders(orders)
    return orders


def _validate_orders(o: OrderSettings) -> None:
    if not o.symbol:
        _die("MT4_SYMBOL kosong")
    if not 1 <= o.port <= 65535:
        _die(f"MT4_BRIDGE_PORT tidak valid: {o.port}")
    loopback = o.bridge_host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not o.remote_allow:
        _die(f"MT4_BRIDGE_HOST={o.bridge_host} membuka port order tanpa autentikasi — "
             "set MT4_REMOTE_ALLOW=1 hanya di jaringan tepercaya")
    if o.call_timeout <= 0:
        _die(f"MT4_CALL_TIMEOUT harus > 0, bukan {o.call_timeout}")
    if o.magic <= 0:
        _die(f"MT4_MAGIC harus > 0, bukan {o.magic}")
    if not 0 < o.risk_pct <= 5:
        _die(f"RISK_PERCENT harus 0<x<=5, bukan {o.risk_pct}")
    if o.max_lot <= 0:
        _die(f"MAX_LOT harus > 0, bukan {o.max_lot}")
    if o.dedupe_seconds <= 0:
        _die(f"ORDER_DEDUPE_SECONDS harus > 0, bukan {o.dedupe_seconds}")
    if o.pending_ttl_s <= 0:
        _die(f"PENDING_TTL_SECONDS harus > 0, bukan {o.pending_ttl_s}")
    if not 0 < o.trail_retrace_pct < 100:
        _die(f"TRAIL_RETRACE_PCT harus 0<x<100, bukan {o.trail_retrace_pct}")
    if o.limit_offset_pct < 0:
        _die(f"LIMIT_OFFSET_PCT tidak boleh negatif: {o.limit_offset_pct}")
    if o.limit_offset_max_pips < 0:
        _die(f"LIMIT_OFFSET_MAX_PIPS tidak boleh negatif: {o.limit_offset_max_pips}")
    if o.layering_min < 2:
        _die(f"LAYERING_MIN minimal 2, bukan {o.layering_min}")
    if not 0 < o.layering_margin_safety <= 1:
        _die(f"LAYERING_MARGIN_SAFETY harus 0<x<=1, bukan {o.layering_margin_safety}")
    if o.layering_max_margin_pct <= 0:
        _die(f"LAYERING_MAX_MARGIN_PCT harus > 0, bukan {o.layering_max_margin_pct}")
    if o.band_atr < 0:
        _die(f"ADVISOR_BAND_ATR tidak boleh negatif: {o.band_atr}")
    if not 0 <= o.advisor_retries <= 3:
        _die(f"ADVISOR_CLI_RETRIES harus 0..3, bukan {o.advisor_retries}")
    if o.max_positions < 1:
        _die(f"MAX_POSITIONS harus >= 1, bukan {o.max_positions}")
    if o.max_drift <= 0:
        _die(f"MAX_DRIFT harus > 0, bukan {o.max_drift}")
    if o.enabled and not o.allow_trading:
        _die("ORDER_ENABLED=1 tapi MT4_ALLOW_TRADING=0 — kontradiksi, "
             "nyalakan keduanya atau matikan ORDER_ENABLED")
    if o.allow_trading and not o.enabled:
        _die("MT4_ALLOW_TRADING=1 tapi ORDER_ENABLED=0 — matikan salah satu")
    if o.advisor_url and not o.advisor_model:
        _die("ADVISOR_URL diisi tapi ADVISOR_MODEL kosong")
    if o.advisor_url and o.advisor_cli_model:
        _die("ADVISOR_URL dan ADVISOR_CLI_MODEL diisi bersamaan — pilih satu backend")
    if o.advisor_timeout <= 0:
        _die(f"ADVISOR_TIMEOUT harus > 0, bukan {o.advisor_timeout}")
