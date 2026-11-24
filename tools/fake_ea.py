"""Simulator EA MT4 — menguji jalur Python tanpa terminal MT4.

Dipakai dua cara:
  python -m tools.fake_ea --port 8766        # manual, mendampingi monitor
  FakeEa(port) + .start()                    # dari pytest

Perilaku meniru EA asli: dial out ke server Python, balas frame pipa, tolak
order bila `allow_trading` false, dan simpan order terkirim untuk verifikasi.
"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
import time

from xau_monitor.mt4_wire import (
    WireError, encode_response, parse_request,
)

log = logging.getLogger("fake_ea")

DEFAULT_SPEC = {
    "digits": 2, "point": 0.01, "ticksize": 0.01, "tickvalue": 1.0,
    "stoplevel": 0, "freezelevel": 0, "lotmin": 0.01, "lotmax": 50.0,
    "lotstep": 0.01, "marginreq": 20.0,
}


class FakeEa:
    def __init__(self, port: int, symbol: str = "XAUUSD", *, bid: float = 2000.0,
                 ask: float = 2000.5, allow_trading: bool = True,
                 magic: int = 20261116, equity: float = 10000.0,
                 free_margin: float = 9000.0, spread_points: int = 20,
                 spec: dict | None = None) -> None:
        self.port = port
        self.symbol = symbol
        self.bid, self.ask = bid, ask
        self.allow_trading = allow_trading
        self.magic = magic
        self.equity = equity
        self.free_margin = free_margin
        self.spread_points = spread_points
        self.spec = {**DEFAULT_SPEC, **(spec or {})}
        self.sent_orders: list[dict] = []
        self.pending_orders: list[dict] = []   # limit yang belum terisi
        self.positions: list[dict] = []
        self.frames: list[str] = []
        self.silent = False
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_ticket = 9001

    # ---- lifecycle -------------------------------------------------
    def start(self, connect_timeout: float = 5.0) -> "FakeEa":
        self._thread = threading.Thread(target=self._run, args=(connect_timeout,),
                                        daemon=True, name="fake-ea")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                log.debug("socket simulator sudah tertutup", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self, connect_timeout: float) -> None:
        deadline = time.time() + connect_timeout
        while time.time() < deadline and not self._stop.is_set():
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
            except OSError:
                time.sleep(0.05)
                continue
            sock.settimeout(None)  # EA nyata tidak putus karena idle
            self._sock = sock
            break
        if self._sock is None:
            log.error("simulator gagal terhubung ke bridge :%d", self.port)
            return
        self._serve(self._sock)

    def _serve(self, sock: socket.socket) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, _, buf = buf.partition(b"\n")
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace")
                    self.frames.append(line)
                    if self.silent:
                        continue
                    self._handle(sock, line)
        except OSError:
            return

    # ---- handlers --------------------------------------------------
    def _handle(self, sock: socket.socket, line: str) -> None:
        try:
            req_id, cmd, params = parse_request(line)
        except WireError as exc:
            log.error("frame rusak: %s", exc)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            sock.sendall(encode_response(req_id, False, err="unknown_cmd", msg=cmd))
            return
        payload = handler(params)
        ok = bool(payload.pop("_ok", True))
        sock.sendall(encode_response(req_id, ok, **payload))

    def _cmd_ping(self, params: dict[str, str]) -> dict:
        return {
            "build": 1, "login": 2098919075, "server": "FakeBroker-Demo",
            "currency": "USD", "equity": self.equity, "balance": self.equity,
            "freemargin": self.free_margin, "leverage": 100,
            "trading_allowed": int(self.allow_trading), "terminal_trade": 1,
            "connected": 1, "server_time": int(time.time()), "frames": len(self.frames),
        }

    def _cmd_symbol_info(self, params: dict[str, str]) -> dict:
        if params.get("sym") != self.symbol:
            return {"_ok": False, "err": "unknown_symbol", "msg": params.get("sym", "")}
        return {
            "sym": self.symbol, "bid": self.bid, "ask": self.ask,
            "spread": self.spread_points, "tradeallowed": 1, **self.spec,
        }

    def _cmd_pending_list(self, params: dict[str, str]) -> dict:
        magic = int(params.get("magic", self.magic))
        rows = [o for o in self.pending_orders
                if o["magic"] == magic]
        return {"count": len(rows),
                "tickets": "~".join(str(o["ticket"]) for o in rows)}

    def _cmd_positions(self, params: dict[str, str]) -> dict:
        magic = int(params.get("magic", self.magic))
        rows = [p for p in self.positions
                if p["symbol"] == params.get("sym") and p["magic"] == magic]
        records = [",".join([
            str(p["ticket"]), p["type"], f"{p['lots']:.2f}", f"{p['open']:.2f}",
            f"{p['sl']:.2f}", f"{p['tp']:.2f}", f"{p['profit']:.2f}", p["comment"],
        ]) for p in rows]
        return {"count": len(rows), "pos": "~".join(records)}

    def _cmd_order_send(self, params: dict[str, str]) -> dict:
        if not self.allow_trading:
            return {"_ok": False, "err": "trading_disabled", "msg": "simulator read-only"}
        if params.get("sym") != self.symbol:
            return {"_ok": False, "err": "unknown_symbol", "msg": params.get("sym", "")}
        sides = {"buy": "buy", "sell": "sell"}
        side = sides.get(params.get("side", ""))
        if side is None:
            return {"_ok": False, "err": "bad_side", "msg": params.get("side", "")}
        ticket = self._next_ticket
        self._next_ticket += 1
        otype = params.get("otype", "market")
        if otype == "limit":
            want = float(params.get("price", 0) or 0)
            if want <= 0:
                return {"_ok": False, "err": "bad_price", "msg": "limit tanpa price"}
            if side == "buy" and want >= self.ask:
                return {"_ok": False, "err": "buy_limit_above_market", "msg": ""}
            if side == "sell" and want <= self.bid:
                return {"_ok": False, "err": "sell_limit_below_market", "msg": ""}
            order = {"ticket": ticket, "side": side, "otype": "limit",
                     "lots": float(params.get("lots", 0)), "price": want,
                     "sl": 0.0, "tp": 0.0,
                     "magic": int(params.get("magic", 0)),
                     "comment": params.get("comment", "")}
            self.sent_orders.append(order)
            self.pending_orders.append(order)   # belum jadi posisi
            return {"ticket": ticket, "price": want, "sl": 0.0, "tp": 0.0,
                    "otype": "limit"}
        price = self.ask if side == "buy" else self.bid
        order = {"ticket": ticket, "side": side, "otype": "market",
                 "lots": float(params.get("lots", 0)),
                 "price": price, "sl": float(params.get("sl", 0)),
                 "tp": float(params.get("tp", 0)), "magic": int(params.get("magic", 0)),
                 "comment": params.get("comment", "")}
        self.sent_orders.append(order)
        self.positions.append({
            "ticket": ticket, "symbol": self.symbol, "type": side,
            "lots": order["lots"], "open": price, "sl": order["sl"], "tp": order["tp"],
            "profit": 0.0, "comment": order["comment"], "magic": order["magic"],
        })
        return {"ticket": ticket, "price": price, "sl": order["sl"], "tp": order["tp"]}

    def fill(self, ticket: int) -> None:
        """Simulasi limit tersentuh: pending -> posisi (ticket sama)."""
        for order in list(self.pending_orders):
            if order["ticket"] != ticket:
                continue
            self.pending_orders.remove(order)
            self.positions.append({
                "ticket": ticket, "symbol": self.symbol, "type": order["side"],
                "lots": order["lots"], "open": order["price"], "sl": 0.0, "tp": 0.0,
                "profit": 0.0, "comment": order["comment"], "magic": order["magic"],
            })
            return

    def _cmd_order_delete(self, params: dict[str, str]) -> dict:
        if not self.allow_trading:
            return {"_ok": False, "err": "trading_disabled", "msg": "simulator read-only"}
        ticket = int(params.get("ticket", 0))
        if any(p["ticket"] == ticket for p in self.positions):
            return {"_ok": False, "err": "is_position", "msg": "bukan pending"}
        before = len(self.pending_orders)
        self.pending_orders = [o for o in self.pending_orders if o["ticket"] != ticket]
        if len(self.pending_orders) == before:
            return {"_ok": False, "err": "ticket_not_found", "msg": str(ticket)}
        return {"ticket": ticket}

    def _cmd_order_close(self, params: dict[str, str]) -> dict:
        if not self.allow_trading:
            return {"_ok": False, "err": "trading_disabled", "msg": "simulator read-only"}
        ticket = int(params.get("ticket", 0))
        before = len(self.positions)
        self.positions = [p for p in self.positions if p["ticket"] != ticket]
        if len(self.positions) == before:
            return {"_ok": False, "err": "ticket_not_found", "msg": str(ticket)}
        return {"ticket": ticket}

    def _cmd_order_modify(self, params: dict[str, str]) -> dict:
        if not self.allow_trading:
            return {"_ok": False, "err": "trading_disabled", "msg": "simulator read-only"}
        ticket = int(params.get("ticket", 0))
        for pos in self.positions:
            if pos["ticket"] == ticket:
                pos["sl"] = float(params.get("sl", pos["sl"]))
                pos["tp"] = float(params.get("tp", pos["tp"]))
                return {"ticket": ticket, "sl": pos["sl"], "tp": pos["tp"]}
        return {"_ok": False, "err": "ticket_not_found", "msg": str(ticket)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulator EA MT4 (tanpa terminal)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--bid", type=float, default=2000.0)
    parser.add_argument("--ask", type=float, default=2000.5)
    parser.add_argument("--no-trade", action="store_true", help="tolak semua order")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ea = FakeEa(args.port, args.symbol, bid=args.bid, ask=args.ask,
                allow_trading=not args.no_trade).start()
    print(f"fake EA menunggu bridge di 127.0.0.1:{args.port} ...", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        ea.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
