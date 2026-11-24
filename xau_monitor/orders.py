"""Order intent + deterministic risk guard (pure, no I/O).

Urutan keputusan yang disengaja: **engine -> AI advisor -> guard ini**.
Guard adalah otoritas terakhir dan tidak bisa dinegosiasikan oleh model:
AI hanya boleh mengusulkan harga, guard yang memutuskan volume, arah, dan
apakah order boleh dikirim sama sekali.

Kebijakan harga (penting): feed kantor (XAUUSD FIX) dan broker MT4 bukan
sumber harga yang sama. Semua level digeser dengan `basis = entry - ref`,
lalu divalidasi ulang. Bila basis melebihi ambang, struktur harga tidak
transferable => tolak (bukan diam-diam digeser jauh).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .mt4_wire import as_float, as_int


class GuardRejection(RuntimeError):
    """Order ditolak guard. `code` stabil untuk audit, `detail` untuk manusia."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    stops_level_points: int
    freeze_level_points: int
    lot_min: float
    lot_max: float
    lot_step: float
    margin_per_lot: float
    spread_points: int
    bid: float
    ask: float
    trade_allowed: bool

    @classmethod
    def from_wire(cls, params: Mapping[str, str]) -> "SymbolSpec":
        spec = cls(
            name=params.get("sym", ""),
            digits=as_int(params, "digits"),
            point=as_float(params, "point"),
            tick_size=as_float(params, "ticksize"),
            tick_value=as_float(params, "tickvalue"),
            stops_level_points=as_int(params, "stoplevel"),
            freeze_level_points=as_int(params, "freezelevel", 0),
            lot_min=as_float(params, "lotmin"),
            lot_max=as_float(params, "lotmax"),
            lot_step=as_float(params, "lotstep"),
            margin_per_lot=as_float(params, "marginreq"),
            spread_points=as_int(params, "spread", 0),
            bid=as_float(params, "bid"),
            ask=as_float(params, "ask"),
            trade_allowed=params.get("tradeallowed") == "1",
        )
        spec._validate()
        return spec

    def _validate(self) -> None:
        if not self.name:
            raise GuardRejection("spec_invalid", "symbol kosong dari EA")
        for field in ("point", "tick_size", "tick_value", "lot_step"):
            if getattr(self, field) <= 0:
                raise GuardRejection("spec_invalid", f"{field}={getattr(self, field)}")
        if self.bid <= 0 or self.ask <= 0:
            raise GuardRejection("spec_invalid", f"quote tidak valid bid={self.bid} ask={self.ask}")
        if self.ask < self.bid:
            raise GuardRejection("spec_invalid", f"ask {self.ask} < bid {self.bid}")


@dataclass(frozen=True)
class AccountState:
    login: int
    server: str
    currency: str
    equity: float
    balance: float
    free_margin: float
    leverage: int

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise GuardRejection("account_invalid", f"equity={self.equity}")
        if self.free_margin < 0:
            raise GuardRejection("account_invalid", f"free_margin={self.free_margin}")

    @classmethod
    def from_wire(cls, params: Mapping[str, str]) -> "AccountState":
        return cls(
            login=as_int(params, "login"),
            server=params.get("server", ""),
            currency=params.get("currency", ""),
            equity=as_float(params, "equity"),
            balance=as_float(params, "balance"),
            free_margin=as_float(params, "freemargin"),
            leverage=as_int(params, "leverage", 0),
        )


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float          # % equity yang boleh hilang bila SL kena
    max_lot: float
    max_spread_points: int
    max_basis: float
    attach_tp: bool
    attach_sl: bool = True   # False = tanpa SL broker (patuh .md), lot = MAX_LOT

    def validate(self) -> None:
        if not 0 < self.risk_pct <= 5:
            raise GuardRejection("config_invalid", f"RISK_PERCENT={self.risk_pct} (harus 0<x<=5)")
        if self.max_lot <= 0:
            raise GuardRejection("config_invalid", f"MAX_LOT={self.max_lot}")
        if self.max_spread_points < 0:
            raise GuardRejection("config_invalid", f"MAX_SPREAD_POINTS={self.max_spread_points}")
        if self.max_basis < 0:
            raise GuardRejection("config_invalid", f"MAX_BASIS={self.max_basis}")


@dataclass(frozen=True)
class OrderIntent:
    """Niat order dari engine — harga masih dalam basis feed kantor."""

    symbol: str
    side: str                 # BUY | SELL
    ref_price: float
    stop: float
    tps: tuple[float, ...]
    invalid: float
    zone: float
    mode: str
    atr_m1: float

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise GuardRejection("intent_invalid", f"side={self.side!r}")
        if self.ref_price <= 0 or self.stop <= 0:
            raise GuardRejection("intent_invalid", "harga ref/stop tidak valid")
        if self.side == "BUY" and not self.stop < self.ref_price:
            raise GuardRejection("intent_invalid", "BUY butuh stop < ref")
        if self.side == "SELL" and not self.stop > self.ref_price:
            raise GuardRejection("intent_invalid", "SELL butuh stop > ref")


@dataclass(frozen=True)
class OrderPlan:
    symbol: str
    side: str
    lots: float
    entry: float
    stop: float | None
    take_profit: float | None
    basis: float
    risk_amount: float
    risk_per_lot: float
    risk_points: float
    reasons: tuple[str, ...]
    lots_requested: float | None = None   # usulan advisor sebelum di-clamp guard


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError(f"lot step harus > 0, bukan {step}")
    return round(math.floor(value / step + 1e-9) * step, 8)


def build_plan(intent: OrderIntent, spec: SymbolSpec, account: AccountState,
               risk: RiskConfig, lots_requested: float | None = None) -> OrderPlan:
    risk.validate()
    if not spec.trade_allowed:
        raise GuardRejection("symbol_disabled", f"{spec.name} tidak bisa ditradingkan broker")
    if spec.spread_points > risk.max_spread_points:
        raise GuardRejection("spread_too_wide",
                             f"spread {spec.spread_points} > {risk.max_spread_points} poin")

    entry = spec.ask if intent.side == "BUY" else spec.bid
    basis = entry - intent.ref_price
    if abs(basis) > risk.max_basis:
        raise GuardRejection("basis_too_wide",
                             f"|{basis:.{spec.digits}f}| > {risk.max_basis} "
                             f"(feed vs broker)")

    stop = round(intent.stop + basis, spec.digits)
    tps = [round(tp + basis, spec.digits) for tp in intent.tps]
    entry = round(entry, spec.digits)
    min_dist = spec.stops_level_points * spec.point

    stop: float | None = None
    if risk.attach_sl:
        stop = round(intent.stop + basis, spec.digits)
        if intent.side == "BUY" and stop >= entry:
            raise GuardRejection("geometry", f"BUY stop {stop} >= entry {entry}")
        if intent.side == "SELL" and stop <= entry:
            raise GuardRejection("geometry", f"SELL stop {stop} <= entry {entry}")
        if abs(entry - stop) < min_dist:
            raise GuardRejection("stop_too_close",
                                 f"jarak SL {abs(entry - stop):.{spec.digits}f} "
                                 f"< stoplevel {min_dist:.{spec.digits}f}")

    tp: float | None = None
    if risk.attach_tp:
        if not tps:
            raise GuardRejection("tp_missing", "ATTACH_TP=1 tapi engine tidak memberi TP")
        # `tps` di atas SUDAH digeser basis; menambah basis lagi = geser ganda.
        tp = round(tps[0], spec.digits)  # .md: TP cepat, tidak serakah
        if intent.side == "BUY" and tp <= entry:
            raise GuardRejection("geometry", f"BUY TP {tp} <= entry {entry}")
        if intent.side == "SELL" and tp >= entry:
            raise GuardRejection("geometry", f"SELL TP {tp} >= entry {entry}")
        if abs(tp - entry) < min_dist:
            raise GuardRejection("tp_too_close",
                                 f"jarak TP {abs(tp - entry):.{spec.digits}f} "
                                 f"< stoplevel {min_dist:.{spec.digits}f}")

    if stop is not None:
        risk_amount = account.equity * risk.risk_pct / 100.0
        sl_distance = abs(entry - stop)
        risk_per_lot = (sl_distance / spec.tick_size) * spec.tick_value
        if risk_per_lot <= 0:
            raise GuardRejection("sizing", f"risk per lot tidak valid ({risk_per_lot})")
        raw_lot = risk_amount / risk_per_lot
        lots = floor_to_step(raw_lot, spec.lot_step)
        lots = min(lots, spec.lot_max, risk.max_lot)
        if lots < spec.lot_min:
            raise GuardRejection("lot_below_min",
                                 f"raw {raw_lot:.4f} < min {spec.lot_min} "
                                 f"(risiko {risk.risk_pct}% terlalu kecil untuk SL ini)")
        risk_points = round(sl_distance / spec.point, 1)
        size_note = f"lot {lots:g} (raw {raw_lot:.4f})"
    else:
        # Tanpa SL broker (patuh .md): risk% tidak bisa dipakai. Exposure dibatasi
        # MAX_LOT dan lot_max broker — bukan oleh jarak SL.
        risk_amount, risk_per_lot, risk_points = 0.0, 0.0, 0.0
        lots = min(spec.lot_max, risk.max_lot)
        lots = floor_to_step(lots, spec.lot_step)
        if lots < spec.lot_min:
            raise GuardRejection("lot_below_min",
                                 f"MAX_LOT {risk.max_lot} < lot_min {spec.lot_min}")
        size_note = f"lot {lots:g} (fixed, tanpa SL)"

    # ---- volume: usulan advisor, KEPUTUSAN AKHIR tetap guard -------------
    # Angka model tidak pernah dipakai mentah: floor ke lotstep, clamp ke
    # lot_min..min(MAX_LOT, lot_max). Di bawah lot_min = ditolak (bukan dinaikkan
    # diam-diam), supaya audit menunjukkan model meminta ukuran tidak valid.
    if lots_requested is not None:
        if not math.isfinite(lots_requested) or lots_requested <= 0:
            raise GuardRejection("advisor_lot_invalid",
                                 f"usulan lots {lots_requested!r} tidak valid")
        cap = min(spec.lot_max, risk.max_lot)
        lots = floor_to_step(min(lots_requested, cap), spec.lot_step)
        if lots < spec.lot_min:
            raise GuardRejection("lot_below_min",
                                 f"advisor minta {lots_requested:g} < lot_min "
                                 f"{spec.lot_min:g}")
        size_note = f"lot {lots:g} (advisor minta {lots_requested:g}, cap {cap:g})"

    if spec.margin_per_lot <= 0:
        raise GuardRejection("margin_unknown", "broker tidak melaporkan MODE_MARGINREQUIRED")
    required_margin = lots * spec.margin_per_lot
    if required_margin > account.free_margin * 0.9:
        raise GuardRejection("margin", f"butuh margin {required_margin:.2f} > 90% free "
                                       f"{account.free_margin:.2f}")

    reasons = (
        f"basis {basis:+.{spec.digits}f}",
        "SL: tidak dipasang (patuh .md)" if stop is None
        else f"SL {abs(entry - stop):.{spec.digits}f} ({risk_points:.0f} poin)",
        "TP: tidak dipasang" if tp is None
        else f"TP {abs(tp - entry):.{spec.digits}f}",
        size_note,
    )
    return OrderPlan(spec.name, intent.side, lots, entry, stop, tp, basis,
                     round(risk_amount, 2), round(risk_per_lot, 4), risk_points,
                     reasons, lots_requested)
