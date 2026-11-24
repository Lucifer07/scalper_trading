"""Logging monitor: console + rotating file (dibaca panel GUI).

File handler wajib karena panel berjalan sebagai proses lain dan tidak boleh
menguras pipe stdout (risiko blocking). Rotasi membatasi ukuran agar tail
panel tetap murah: O(delta) per polling, bukan O(file).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured: str | None = None


def configure(log_path: str, max_bytes: int = 2_000_000, backups: int = 5) -> str:
    """Pasang handler console + rotasi file. Idempoten untuk path yang sama."""
    global _configured
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(FORMAT)
    if _configured == log_path:
        return log_path

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_path:
        target = Path(log_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(target, maxBytes=max_bytes,
                                           backupCount=backups, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _configured = log_path
    else:
        _configured = ""
    return log_path
