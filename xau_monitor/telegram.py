"""Telegram notifier — stdlib HTTPS only (no SDK dependency).

Never raises: returns bool and logs loudly. A failed alert must be visible,
not swallowed (fail-loudly principle) — main() surfaces failures to console.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
RETRIES = (1.0, 3.0, 9.0)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat = chat_id

    def _call(self, method: str, payload: dict, timeout: float = 15.0) -> dict | None:
        url = API.format(token=self._token, method=method)
        body = json.dumps(payload).encode()
        for attempt, delay in enumerate(RETRIES):
            try:
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
            except Exception as e:  # noqa: BLE001 — must not kill the monitor
                log.error("telegram %s gagal (percobaan %d/%d): %s",
                          method, attempt + 1, len(RETRIES), e)
                if attempt + 1 < len(RETRIES):
                    time.sleep(delay)
        return None

    def send(self, text: str) -> bool:
        resp = self._call("sendMessage", {
            "chat_id": self._chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        ok = bool(resp and resp.get("ok"))
        if not ok:
            log.error("sendMessage tidak ok: %s", resp)
        return ok

    def health(self) -> str | None:
        """Validate token at startup; returns bot username or None."""
        resp = self._call("getMe", {})
        if resp and resp.get("ok"):
            return resp["result"].get("username")
        return None
