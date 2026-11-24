"""Klien tipis control API monitor (:8772). Semua panggilan punya timeout.

Panel tidak boleh menggantung karena monitor mati: kegagalan jaringan selalu
dikembalikan sebagai pesan error, bukan exception yang lolos ke handler HTTP.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

GUARD_HEADER = "X-Xau-Control"


class ControlClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8772,
                 timeout: float = 2.5) -> None:
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def status(self) -> tuple[dict | None, str]:
        return self._get("/api/status")

    def report(self, kind: str) -> tuple[str | None, str]:
        data, err = self._get(f"/api/report?kind={urllib.parse.quote(kind)}")
        if err:
            return None, err
        if not isinstance(data, dict) or "text" not in data:
            return None, "respons report tidak valid"
        return str(data["text"]), ""

    def set_symbol(self, symbol: str) -> tuple[dict | None, str]:
        return self._post("/api/symbol", {"symbol": symbol})

    def set_layering(self, enabled: bool) -> tuple[dict | None, str]:
        return self._post("/api/layering", {"enabled": enabled})

    def _post(self, path: str, payload: dict) -> tuple[dict | None, str]:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "application/json", GUARD_HEADER: "1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp), ""
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError):
                payload = {"error": f"HTTP {exc.code}"}
            return payload, ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"control API tidak terjangkau: {exc}"

    def _get(self, path: str) -> tuple[dict | None, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as resp:
                data = json.load(resp)
            if not isinstance(data, dict):
                return None, "respons bukan objek JSON"
            return data, ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"control API tidak terjangkau: {exc}"
