"""MCP server MT4 (stdio) — alat operator/agen, bukan jalur 24/7.

Jalur otomatis (monitor -> advisor -> guard -> order) ada di `executor.py`.
Server ini untuk pertanyaan manual/forensik: quote, spesifikasi simbol, posisi,
dan (hanya bila di-arm) order/close/modify. Order di sini **tidak** memakai
sizing risiko — lot wajib eksplisit dan divalidasi terhadap batas broker.

Catatan bentrok port: EA hanya bisa tersambung ke SATU bridge. Jalankan MCP ini
hanya saat monitor tidak memegang port yang sama (mis. monitor dihentikan).

Tanpa dependensi SDK: JSON-RPC 2.0 newline-delimited di stdio.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from xau_monitor.mt4_link import Mt4Error, Mt4Link, Mt4Rejected
from xau_monitor.orders import SymbolSpec

log = logging.getLogger("mcp_mt4")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "mt4-bridge", "version": "1.0"}

_READ_TOOLS = [
    {
        "name": "mt4_ping",
        "description": "Status terminal MT4: akun, equity, apakah trading di-arm EA.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "mt4_quote",
        "description": "Bid/ask/spread terakhir untuk simbol (mis. XAUUSD).",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "mt4_symbol",
        "description": "Spesifikasi broker: digits, tick value, stoplevel, lot step, margin/lot.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "mt4_positions",
        "description": "Posisi terbuka milik magic ini (atau semua bila magic=0).",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "magic": {"type": "integer"}},
            "required": ["symbol"],
        },
    },
]

_WRITE_TOOLS = [
    {
        "name": "mt4_order",
        "description": "Order market dengan SL/TP eksplisit. Butuh --allow-trading DAN EA di-arm.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "symbol": {"type": "string"},
                "lots": {"type": "number"},
                "sl": {"type": "number"},
                "tp": {"type": "number"},
                "comment": {"type": "string"},
            },
            "required": ["side", "symbol", "lots", "sl", "tp"],
        },
    },
    {
        "name": "mt4_close",
        "description": "Tutup posisi by ticket (lot opsional untuk partial close).",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket": {"type": "integer"}, "lots": {"type": "number"}},
            "required": ["ticket"],
        },
    },
    {
        "name": "mt4_modify",
        "description": "Ubah SL/TP posisi by ticket.",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket": {"type": "integer"}, "sl": {"type": "number"},
                           "tp": {"type": "number"}},
            "required": ["ticket"],
        },
    },
]


class McpServer:
    def __init__(self, link: Mt4Link, symbol: str, magic: int,
                 allow_trading: bool, deviation: int = 30) -> None:
        self._link = link
        self._symbol = symbol
        self._magic = magic
        self._allow_trading = allow_trading
        self._deviation = deviation

    # ---- tools -----------------------------------------------------
    def tools(self) -> list[dict]:
        return _READ_TOOLS + (_WRITE_TOOLS if self._allow_trading else [])

    def call_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "mt4_ping":
            return _kv(self._link.ping())
        if name == "mt4_quote":
            info = self._link.symbol_info(_sym(args, self._symbol))
            return _kv({k: info.get(k) for k in ("sym", "bid", "ask", "spread")})
        if name == "mt4_symbol":
            spec = SymbolSpec.from_wire(self._link.symbol_info(_sym(args, self._symbol)))
            return _kv({
                "symbol": spec.name, "digits": spec.digits, "point": spec.point,
                "tick_size": spec.tick_size, "tick_value": spec.tick_value,
                "stops_level_points": spec.stops_level_points,
                "lot_min": spec.lot_min, "lot_max": spec.lot_max,
                "lot_step": spec.lot_step, "margin_per_lot": spec.margin_per_lot,
                "spread_points": spec.spread_points, "trade_allowed": spec.trade_allowed,
            })
        if name == "mt4_positions":
            rows = self._link.positions(_sym(args, self._symbol),
                                        int(args.get("magic", self._magic)))
            return json.dumps(rows, ensure_ascii=False)
        if name == "mt4_order":
            return self._order(args)
        if name == "mt4_close":
            return _kv(self._link.close_position(int(args["ticket"]),
                                                 float(args.get("lots", 0.0))))
        if name == "mt4_modify":
            return _kv(self._link.modify(int(args["ticket"]), float(args["sl"]),
                                         float(args["tp"])))
        raise ValueError(f"tool tidak dikenal: {name}")

    def _order(self, args: dict[str, Any]) -> str:
        if not self._allow_trading:
            raise ValueError("mt4_order butuh --allow-trading di server MCP")
        symbol = _sym(args, self._symbol)
        side = str(args["side"]).lower()
        lots = float(args["lots"])
        sl = float(args["sl"])
        tp = float(args["tp"])
        spec = SymbolSpec.from_wire(self._link.symbol_info(symbol))
        if not spec.trade_allowed:
            raise ValueError(f"{symbol} tidak bisa ditradingkan broker")
        if not spec.lot_min <= lots <= spec.lot_max:
            raise ValueError(f"lot {lots} di luar batas {spec.lot_min}-{spec.lot_max}")
        entry = spec.ask if side == "buy" else spec.bid
        if (side == "buy" and not sl < entry < tp) or (side == "sell" and not tp < entry < sl):
            raise ValueError(f"geometri salah: {side} entry {entry} sl {sl} tp {tp}")
        return _kv(self._link.order_send(
            side=side, symbol=symbol, lots=lots, sl=sl, tp=tp, magic=self._magic,
            deviation=self._deviation, comment=str(args.get("comment", "mcp-mt4"))))

    # ---- JSON-RPC --------------------------------------------------
    def handle(self, req: dict) -> dict | None:
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return _ok(rid, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            return _ok(rid, {"tools": self.tools()})
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = self.call_tool(str(name), args)
            except (Mt4Rejected, Mt4Error, ValueError, KeyError, TypeError) as exc:
                return _ok(rid, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                 "isError": True})
            return _ok(rid, {"content": [{"type": "text", "text": text}]})
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method tidak didukung: {method}"}}


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _sym(args: dict, default: str) -> str:
    return str(args.get("symbol") or default).upper()


def _kv(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def serve(server: McpServer) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        resp = server.handle(req)
        if resp is not None:
            _emit(resp)
    return 0


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP server untuk bridge MT4")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--magic", type=int, default=20261116)
    parser.add_argument("--deviation", type=int, default=30)
    parser.add_argument("--allow-trading", action="store_true",
                        help="buka tool order/close/modify (EA tetap harus di-arm)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        link = Mt4Link(args.port, call_timeout=6.0)
    except OSError as exc:
        log.error("port %d tidak bisa dipakai: %s (monitor masih jalan?)", args.port, exc)
        return 2
    try:
        return serve(McpServer(link, args.symbol, args.magic, args.allow_trading,
                               args.deviation))
    finally:
        link.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
