"""Primitif baca environment: .env loader + parser tervalidasi (fail-loudly).

Dipisah dari `config` supaya blok order-settings bisa memakai parser yang sama
tanpa import melingkar. Tidak ada nilai default bisnis di sini — hanya parsing.
"""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=value lines, '#' comments, no interpolation."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _die(msg: str) -> None:
    raise SystemExit(f"[config] FATAL: {msg}")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError as exc:
        _die(f"{name} bukan angka: {exc}")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except ValueError as exc:
        _die(f"{name} bukan angka: {exc}")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val not in ("0", "1", "true", "false"):
        _die(f"{name} harus 0/1, bukan {raw!r}")
    return val in ("1", "true")
