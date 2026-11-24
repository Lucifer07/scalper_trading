"""HTTP panel GUI (loopback-only): API JSON + aset statis tanpa dependency.

Mutasi (POST/PUT) menuntut header `X-Xau-Panel: 1` agar halaman web asing tidak
bisa memicu CSRF ke localhost. Tidak ada CORS — preflight asing ditolak.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .files import FileError, FileRegistry
from .logs import LogTail
from .supervisor import Supervisor
from .targets import Target

log = logging.getLogger("panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/core.js": ("core.js", "text/javascript; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/symbol.js": ("symbol.js", "text/javascript; charset=utf-8"),
    "/logs.js": ("logs.js", "text/javascript; charset=utf-8"),
    "/files.js": ("files.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
GUARD_HEADER = "X-Xau-Panel"
MAX_BODY = 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")


class PanelApp:
    """Panel multi-instance: parameter `instance` memilih monitor yang dibaca.

    Registry simbol -> (control API, log) datang dari symbols.json (targets.py).
    """

    def __init__(self, supervisor: Supervisor, files: FileRegistry,
                 stdio_logs: LogTail, targets: dict[str, Target],
                 default_instance: str) -> None:
        self.supervisor = supervisor
        self.files = files
        self.stdio_logs = stdio_logs
        self.targets = targets
        self.default_instance = default_instance
        self.started_at = time.time()

    # ---- routing ---------------------------------------------------
    def handle(self, method: str, path: str, query: dict[str, str],
               body: bytes, headers) -> tuple[int, dict | bytes, str]:
        if method == "GET":
            return self._get(path, query)
        if method not in ("POST", "PUT"):
            return 405, {"error": "method tidak didukung"}, "json"
        if headers.get(GUARD_HEADER, "") != "1":
            return 403, {"error": f"header {GUARD_HEADER}: 1 wajib"}, "json"
        try:
            payload = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return 400, {"error": f"body JSON tidak valid: {exc}"}, "json"
        if not isinstance(payload, dict):
            return 400, {"error": "body harus objek JSON"}, "json"
        return self._mutate(method, path, payload, query)

    def _target(self, query: dict[str, str]) -> tuple[Target | None, str]:
        inst = (query.get("instance") or self.default_instance).strip().upper()
        return self.targets.get(inst), inst

    def _get(self, path: str, query: dict[str, str]) -> tuple[int, dict | bytes, str]:
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                return 200, (STATIC_DIR / name).read_bytes(), ctype
            except OSError:
                return 500, {"error": f"aset {name} tidak ada"}, "json"
        if path == "/api/health":
            return 200, {"ok": True, "uptime_s": round(time.time() - self.started_at, 1)}, "json"
        if path == "/api/symbols":
            return 200, {"instances": list(self.targets),
                         "default": self.default_instance}, "json"
        target, inst = self._target(query)
        if target is None:
            return 404, {"error": f"instance {inst} tidak terdaftar"}, "json"
        if path == "/api/state":
            return self._state(target)
        if path == "/api/report":
            kind = query.get("kind", "status")
            if kind not in ("status", "planning", "help"):
                return 400, {"error": "kind harus status/planning/help"}, "json"
            text, err = target.control.report(kind)
            if err:
                return 503, {"error": err}, "json"
            return 200, {"kind": kind, "text": text}, "json"
        if path == "/api/logs":
            tail = self._tail(query.get("source", "monitor"), target)
            cursor = _int(query.get("cursor"), 0)
            limit = _int(query.get("limit"), 300)
            return 200, tail.read(cursor or None, limit), "json"
        if path == "/api/logs/download":
            tail = self._tail(query.get("source", "monitor"), target)
            dl_path = tail.download_path()
            if dl_path is None:
                return 404, {"error": "log belum ada"}, "json"
            return 200, dl_path.read_bytes(), "download"
        if path == "/api/files":
            return 200, {"root": str(self.files.root), "files": self.files.list()}, "json"
        if path == "/api/file":
            file_id = query.get("id", "")
            try:
                return 200, self.files.read(file_id), "json"
            except FileError as exc:
                return exc.code, {"error": str(exc)}, "json"
        return 404, {"error": "not found"}, "json"

    def _mutate(self, method: str, path: str, payload: dict,
                query: dict[str, str]) -> tuple[int, dict | bytes, str]:
        if path.startswith("/api/monitor/"):
            action = path.rsplit("/", 1)[-1]
            if action not in ("start", "stop", "restart"):
                return 404, {"error": "aksi monitor tidak dikenal"}, "json"
            result = getattr(self.supervisor, action)()
            return 200, result, "json"
        target, inst = self._target(query)
        if target is None:
            return 404, {"error": f"instance {inst} tidak terdaftar"}, "json"
        if method == "POST" and path == "/api/symbol":
            symbol = payload.get("symbol")
            if not isinstance(symbol, str):
                return 400, {"error": "field 'symbol' wajib"}, "json"
            result, err = target.control.set_symbol(symbol)
            if err:
                return 503, {"error": err}, "json"
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            return 200 if ok else 409, result, "json"
        if method == "POST" and path == "/api/layering":
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                return 400, {"error": "field 'enabled' (bool) wajib"}, "json"
            result, err = target.control.set_layering(enabled)
            if err:
                return 503, {"error": err}, "json"
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            return 200 if ok else 409, result, "json"
        if method == "PUT" and path == "/api/file":
            return self._save_file(payload)
        return 404, {"error": "not found"}, "json"

    # ---- handlers --------------------------------------------------
    def _state(self, target: Target) -> tuple[int, dict, str]:
        status, err = target.control.status()
        monitor = self.supervisor.state()
        return 200, {
            "monitor": monitor,
            "control": {"ok": not err, "error": err},
            "status": status,
            "server_time": round(time.time(), 1),
        }, "json"

    def _save_file(self, payload: dict) -> tuple[int, dict, str]:
        file_id = payload.get("id")
        content = payload.get("content")
        if not isinstance(file_id, str) or not isinstance(content, str):
            return 400, {"error": "id dan content (string) wajib"}, "json"
        mtime_ns = payload.get("mtime_ns")
        try:
            result = self.files.write(
                file_id, content,
                mtime_ns=int(mtime_ns) if isinstance(mtime_ns, (int, float)) else None,
                force=bool(payload.get("force")))
        except FileError as exc:
            return exc.code, {"error": str(exc)}, "json"
        log.info("file %s disimpan via panel (%d byte)", file_id, result.get("size", 0))
        return 200, result, "json"

    def _tail(self, source: str, target: Target) -> LogTail:
        return self.stdio_logs if source == "stdio" else target.log


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], app: PanelApp) -> None:
        super().__init__(addr, _Handler)
        self.app = app


class _Handler(BaseHTTPRequestHandler):
    server_version = "xau-panel/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        if not self._allowed():
            return self._send(403, {"error": "hanya boleh dari loopback dengan Host lokal"}, "json")
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
        body = b""
        if method in ("POST", "PUT"):
            try:
                body = self._read_body()
            except ValueError as exc:
                return self._send(400, {"error": str(exc)}, "json")
        try:
            code, payload, kind = self.server.app.handle(
                method, parts.path, query, body, self.headers)
        except FileError as exc:                     # jaring pengaman terakhir
            code, payload, kind = exc.code, {"error": str(exc)}, "json"
        except Exception as exc:                     # noqa: BLE001 — panel harus tetap hidup
            log.exception("handler panel gagal: %s %s", method, parts.path)
            code, payload, kind = 500, {"error": f"internal: {exc}"}, "json"
        self._send(code, payload, kind)

    def _allowed(self) -> bool:
        if not self._loopback():
            return False
        raw = self.headers.get("Host", "")
        name = (urlsplit("//" + raw).hostname or "").lower()
        return name in ("127.0.0.1", "localhost", "::1")

    def _loopback(self) -> bool:
        import ipaddress
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

    def _send(self, code: int, payload, kind: str) -> None:
        if kind == "json":
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif kind == "download":
            raw = payload
            ctype = "text/plain; charset=utf-8"
        else:
            raw = payload
            ctype = kind
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store" if kind == "json" else "max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        if code >= 400:                 # body mungkin belum dibaca — jangan keep-alive
            self.close_connection = True
            self.send_header("Connection", "close")
        if kind == "download":
            self.send_header("Content-Disposition", 'attachment; filename="xau-monitor.log"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)


def _int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default
