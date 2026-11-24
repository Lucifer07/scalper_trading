"""Control API monitor (loopback-only) untuk panel GUI.

Kenapa HTTP kecil di dalam monitor, bukan panel membaca log: sumber kebenaran
status (harga, state engine, sumber tick) hanya ada di proses monitor. Panel
cukup memanggil endpoint ini; tidak ada parsing log yang rapuh.

Keamanan: bind wajib loopback (divalidasi di config). Mutasi (`POST`) menuntut
header `X-Xau-Control: 1` + `Content-Type: application/json` sehingga halaman
web asing tidak bisa memicu CSRF ke 127.0.0.1 (preflight tanpa CORS ditolak).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("control")

MAX_BODY = 8192
GUARD_HEADER = "X-Xau-Control"
REPORT_KINDS = ("status", "planning", "help")


class ControlServer:
    def __init__(self, host: str, port: int, status_fn,
                 report_fn=None, symbol_fn=None, layering_fn=None) -> None:
        self._status_fn = status_fn
        self._report_fn = report_fn
        self._symbol_fn = symbol_fn
        self._layering_fn = layering_fn
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.app = self  # type: ignore[attr-defined] — handler menyentuh app
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="control-http", daemon=True)
        self.addr = self._httpd.server_address[:2]
        self.error = ""

    def start(self) -> bool:
        try:
            self._thread.start()
        except RuntimeError as exc:
            self.error = f"thread control gagal start: {exc}"
            log.error("%s", self.error)
            return False
        log.info("control API di http://%s:%d (loopback)", *self.addr)
        return True

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # ---- endpoint --------------------------------------------------
    def status(self) -> dict:
        return self._status_fn()

    def report(self, kind: str) -> str:
        if self._report_fn is None:
            raise KeyError(kind)
        return self._report_fn(kind)

    def set_symbol(self, symbol: str) -> tuple[bool, str]:
        if self._symbol_fn is None:
            raise KeyError(symbol)
        return self._symbol_fn(symbol)

    def set_layering(self, enabled: bool) -> tuple[bool, str]:
        if self._layering_fn is None:
            raise KeyError("layering")
        return self._layering_fn(enabled)


class _Handler(BaseHTTPRequestHandler):
    server_version = "xau-control/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- GET -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — nama wajib dari stdlib
        if not self._allowed():
            return self._send(403, {"error": "loopback only"})
        path, _, query = self.path.partition("?")
        if path == "/api/health":
            return self._send(200, {"ok": True, "service": "xau-monitor"})
        if path == "/api/status":
            return self._send(200, self.server.app.status())
        if path == "/api/report":
            kind = _query(query).get("kind", "status")
            if kind not in REPORT_KINDS:
                return self._send(400, {"error": f"kind harus {', '.join(REPORT_KINDS)}"})
            try:
                return self._send(200, {"kind": kind, "text": self.server.app.report(kind)})
            except KeyError:
                return self._send(503, {"error": "report belum siap"})
        return self._send(404, {"error": "not found"})

    # ---- POST ------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            return self._send(403, {"error": "loopback only"})
        if self.headers.get(GUARD_HEADER, "") != "1":
            return self._send(403, {"error": f"header {GUARD_HEADER}: 1 wajib"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, {"error": "Content-Type harus application/json"})
        try:
            body = self._read_body()
            data = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._send(400, {"error": f"body JSON tidak valid: {exc}"})
        path = self.path.partition("?")[0]
        if path == "/api/layering":
            enabled = data.get("enabled") if isinstance(data, dict) else None
            if not isinstance(enabled, bool):
                return self._send(400, {"error": "field 'enabled' (bool) wajib"})
            try:
                ok, msg = self.server.app.set_layering(enabled)
            except KeyError:
                return self._send(503, {"error": "layering tidak tersedia"})
            return self._send(200 if ok else 409, {"ok": ok, "message": msg})
        if path == "/api/symbol":
            symbol = data.get("symbol") if isinstance(data, dict) else None
            if not isinstance(symbol, str):
                return self._send(400, {"error": "field 'symbol' (string) wajib"})
            try:
                ok, msg = self.server.app.set_symbol(symbol)
            except KeyError:
                return self._send(503, {"error": "perpindahan simbol tidak tersedia"})
            return self._send(200 if ok else 409, {"ok": ok, "message": msg})
        return self._send(404, {"error": "not found"})

    # ---- util ------------------------------------------------------
    def _allowed(self) -> bool:
        """Loopback client DAN nama Host lokal (blokir DNS rebinding)."""
        if not self._loopback():
            return False
        return _host_ok(self.headers.get("Host", ""))

    def _loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length tidak valid") from None
        if length < 0 or length > MAX_BODY:
            raise ValueError(f"body melebihi {MAX_BODY} byte")
        return self.rfile.read(length)

    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code >= 400:                 # body mungkin belum dibaca — jangan keep-alive
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)


def _query(raw: str) -> dict[str, str]:
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(raw).items() if v}


def _host_ok(raw: str) -> bool:
    """Host header harus nama loopback — cegah DNS rebinding ke 127.0.0.1."""
    from urllib.parse import urlsplit
    name = (urlsplit("//" + raw).hostname or "").lower()
    return name in ("127.0.0.1", "localhost", "::1")
