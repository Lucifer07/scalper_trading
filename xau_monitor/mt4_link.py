"""MT4 EA link — the EA dials OUT to this server, we never dial into MT4.

Kenapa outbound (EA sebagai TCP client): di MQL4, `connect()` non-blocking +
satu socket jauh lebih sedikit kode berisiko daripada `bind/listen/accept`
dengan `fd_set` buatan tangan. Sisi Python bisa diuji penuh tanpa MT4.

Server ini satu koneksi EA pada satu waktu. `call()` sinkron (dipakai loop
monitor yang memang sinkron); thread pembaca menerjemahkan frame balik.

Fail loudly: EA putus/timeout => `Mt4Unavailable`; EA menolak (`ok=0`) =>
`Mt4Rejected`. Tidak ada retry diam-diam — order tidak boleh diulang tanpa
keputusan eksplisit.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import socket
import threading
import time

from .mt4_wire import (
    WireError, as_float, as_int, encode_request, ok_of, parse_response,
)

log = logging.getLogger("mt4_link")

MAX_LINE_BYTES = 64 * 1024


class Mt4Error(RuntimeError):
    """Kesalahan bridge MT4 (base)."""


class Mt4Unavailable(Mt4Error):
    """EA tidak terhubung / timeout / socket putus."""


class Mt4Rejected(Mt4Error):
    """EA menjawab ok=0 (validasi broker/terminal menolak perintah)."""

    def __init__(self, cmd: str, params: dict[str, str]) -> None:
        self.cmd = cmd
        self.params = params
        self.code = params.get("err", "unknown")
        super().__init__(f"{cmd} ditolak EA: {self.code} {params.get('msg', '')}".strip())


class Mt4Link:
    def __init__(self, port: int, host: str = "127.0.0.1",
                 call_timeout: float = 6.0) -> None:
        self._srv = socket.create_server((host, port))
        self._srv.settimeout(1.0)
        self.port = int(self._srv.getsockname()[1])
        self._call_timeout = call_timeout
        self._sock: socket.socket | None = None
        self._pending: dict[int, cf.Future] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_seen = 0.0
        self._rx_frames = 0
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="mt4-accept")
        self._thread.start()

    # ---- lifecycle -------------------------------------------------
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._sock is not None

    @property
    def last_seen(self) -> float:
        return self._last_seen

    @property
    def rx_frames(self) -> int:
        return self._rx_frames

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            self._drop(sock, "shutdown")
        try:
            self._srv.close()
        except OSError:
            log.debug("server socket sudah tertutup", exc_info=True)

    # ---- transport -------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                busy = self._sock is not None
                if not busy:
                    self._sock = conn
            if busy:
                log.error("EA kedua dari %s ditolak — hanya satu EA per bridge", addr)
                conn.close()
                continue
            log.warning("EA MT4 terhubung dari %s", addr)
            threading.Thread(target=self._read_loop, args=(conn,), daemon=True,
                             name="mt4-read").start()

    def _read_loop(self, conn: socket.socket) -> None:
        buf = b""
        reason = "EOF"
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE_BYTES and b"\n" not in buf:
                    reason = "frame terlalu panjang"
                    break
                while b"\n" in buf:
                    raw, _, buf = buf.partition(b"\n")
                    if raw:
                        self._dispatch(raw.decode("utf-8", "replace"))
        except OSError as exc:
            reason = f"socket error: {exc}"
        finally:
            self._drop(conn, reason)

    def _dispatch(self, line: str) -> None:
        try:
            req_id, params = parse_response(line)
        except WireError as exc:
            log.error("frame rusak dari EA: %s | %r", exc, line[:160])
            return
        with self._lock:
            fut = self._pending.pop(req_id, None)
        self._rx_frames += 1
        if fut is None or fut.done():
            log.debug("respons basi id=%s (request sudah timeout)", req_id)
            return
        fut.set_result((params, time.time()))

    def _drop(self, conn: socket.socket, reason: str) -> None:
        with self._lock:
            if self._sock is conn:
                self._sock = None
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(Mt4Unavailable(f"EA MT4 putus: {reason}"))
        try:
            conn.close()
        except OSError:
            pass
        log.warning("koneksi EA ditutup (%s)", reason)

    def call(self, cmd: str, timeout: float | None = None, **params: object) -> dict[str, str]:
        deadline = self._call_timeout if timeout is None else timeout
        fut: cf.Future = cf.Future()
        with self._lock:
            if self._sock is None:
                raise Mt4Unavailable("EA MT4 belum terhubung ke bridge")
            self._next_id += 1
            req_id = self._next_id
            self._pending[req_id] = fut
            sock = self._sock
            payload = encode_request(req_id, cmd, **params)
        try:
            sock.sendall(payload)
        except OSError as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise Mt4Unavailable(f"kirim {cmd} gagal: {exc}") from exc
        try:
            response, seen = fut.result(timeout=deadline)
        except cf.TimeoutError as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise Mt4Unavailable(f"{cmd} timeout {deadline:.1f}s") from exc
        self._last_seen = seen
        if not ok_of(response):
            raise Mt4Rejected(cmd, dict(response))
        return response

    # ---- typed helpers ---------------------------------------------
    def ping(self) -> dict[str, str]:
        return self.call("ping")

    def symbol_info(self, symbol: str,
                    timeout: float | None = None) -> dict[str, str]:
        return self.call("symbol_info", timeout=timeout, sym=symbol)

    def positions(self, symbol: str, magic: int,
                  timeout: float | None = None) -> list[dict[str, object]]:
        resp = self.call("positions", timeout=timeout, sym=symbol, magic=magic)
        count = as_int(resp, "count", 0)
        raw = resp.get("pos", "")
        records = [r for r in raw.split("~") if r] if raw else []
        if len(records) != count:
            raise Mt4Unavailable(f"positions rusak: count={count} tapi {len(records)} record")
        return [_parse_position(rec) for rec in records]

    def order_send(self, *, side: str, symbol: str, lots: float, sl: float,
                   tp: float, magic: int, deviation: int, comment: str,
                   price: float | None = None,
                   otype: str = "market") -> dict[str, str]:
        params: dict[str, object] = dict(side=side.lower(), sym=symbol,
                                         lots=lots, sl=sl, tp=tp, magic=magic,
                                         dev=deviation, comment=comment)
        if otype != "market":
            params["otype"] = otype
        if price is not None:
            params["price"] = price
        return self.call("order_send", **params)

    def pending_tickets(self, symbol: str, magic: int,
                        timeout: float | None = None) -> list[int]:
        resp = self.call("pending_list", timeout=timeout, sym=symbol, magic=magic)
        raw = resp.get("tickets", "")
        return [int(x) for x in raw.split("~") if x.strip()]

    def order_delete(self, ticket: int,
                     timeout: float | None = None) -> dict[str, str]:
        return self.call("order_delete", timeout=timeout, ticket=ticket)

    def close_position(self, ticket: int, lots: float,
                       timeout: float | None = None) -> dict[str, str]:
        return self.call("order_close", timeout=timeout, ticket=ticket, lots=lots)

    def modify(self, ticket: int, sl: float, tp: float) -> dict[str, str]:
        return self.call("order_modify", ticket=ticket, sl=sl, tp=tp)


_FIELDS = ("ticket", "type", "lots", "open", "sl", "tp", "profit", "comment")


def _parse_position(record: str) -> dict[str, object]:
    parts = record.split(",")
    if len(parts) != len(_FIELDS):
        raise Mt4Unavailable(f"record posisi tidak valid: {record!r}")
    raw = dict(zip(_FIELDS, parts))
    return {
        "ticket": as_int(raw, "ticket"),
        "type": raw["type"],
        "lots": as_float(raw, "lots"),
        "open": as_float(raw, "open"),
        "sl": as_float(raw, "sl"),
        "tp": as_float(raw, "tp"),
        "profit": as_float(raw, "profit"),
        "comment": raw["comment"],
    }
