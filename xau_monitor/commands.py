"""Telegram command listener: /status dan /planning via long-poll getUpdates.

Keamanan: hanya merespon chat_id yang terkonfigurasi; update dari chat lain
diabaikan. Thread daemon — kegagalan polling tidak mematikan monitor.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("commands")
API = "https://api.telegram.org/bot{token}/{method}"
VALID = ("status", "planning", "start", "help")


def parse_command(text: str) -> str:
    """'/status@my_bot' -> 'status'; teks bebas -> ''."""
    if not text or not text.strip():
        return ""
    first = text.strip().split()[0]
    cmd = first.lstrip("/").split("@")[0].lower()
    return cmd if cmd in VALID else ""


class CommandListener(threading.Thread):
    def __init__(self, token: str, chat_id: str, notifier,
                 provider, poll_timeout: int = 25) -> None:
        super().__init__(daemon=True, name="tg-commands")
        self._token = token
        self.chat_id = chat_id
        self._notifier = notifier
        self._provider = provider      # provider(cmd:str) -> str (HTML reply)
        self._poll_timeout = poll_timeout
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _get_updates(self, offset: int | None) -> list[dict]:
        params = f"timeout={self._poll_timeout}"
        if offset is not None:
            params += f"&offset={offset}"
        url = API.format(token=self._token, method="getUpdates") + "?" + params
        with urllib.request.urlopen(url, timeout=self._poll_timeout + 15) as r:
            data = json.load(r)
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates: {data}")
        return data.get("result") or []

    def run(self) -> None:
        log.info("listener aktif: /status /planning (chat %s)", self.chat_id)
        offset = None
        while not self._stop.is_set():
            try:
                updates = self._get_updates(offset)
            except Exception as e:  # noqa: BLE001 — jangan matikan monitor
                log.error("getUpdates gagal: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                    continue  # chat asing — abaikan diam-diam
                cmd = parse_command(msg.get("text") or "")
                if not cmd:
                    continue
                try:
                    reply = self._provider(cmd)
                except Exception as e:  # noqa: BLE001
                    log.error("provider '%s' gagal: %s", cmd, e)
                    reply = "⚠️ error internal — cek journal xau-monitor."
                ok = self._notifier.send(reply)
                log.info("command /%s dibalas (ok=%s)", cmd, ok)
