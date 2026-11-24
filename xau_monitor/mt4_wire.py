"""MT4 EA bridge wire format — one message per line, pipe framed.

Request  (Python -> EA): ``<id>|<cmd>|key=value;key=value``
Response (EA -> Python): ``<id>|ok=1|key=value;...``
                         ``<id>|ok=0|err=<code>;msg=<text>``

Kenapa bukan JSON: MQL4 tidak punya parser JSON, dan EA berjalan di thread
terminal yang sama dengan chart — parser buatan tangan di MQL4 adalah sumber
bug yang tidak bisa diuji di sini. Format pipa datar bisa diparsing dengan
`StringSplit` saja (O(n)), dan sisi Python tetap bisa memvalidasi ketat.

Aturan nilai: tanpa ``|``, ``;``, ``=`` (untuk key) dan newline. Semua nilai
disanitasi di boundary (`clean`), jadi karakter terlarang menjadi ``_``.
"""
from __future__ import annotations

import re
from typing import Mapping

_CMD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FORBIDDEN = ("|", ";", "\r", "\n")


class WireError(ValueError):
    """Pesan tidak sesuai format — selalu fatal untuk pesan itu, bukan diabaikan."""


def clean(value: object) -> str:
    text = str(value)
    for bad in _FORBIDDEN:
        text = text.replace(bad, "_")
    return text


def fmt(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise WireError(f"nilai float tidak valid: {value!r}")
        return f"{value:.10g}"
    return clean(value)


def encode_request(req_id: int, cmd: str, **params: object) -> bytes:
    if req_id <= 0:
        raise WireError(f"id harus > 0, bukan {req_id!r}")
    if not _CMD_RE.match(cmd):
        raise WireError(f"cmd tidak valid: {cmd!r}")
    parts = [str(req_id), cmd]
    if params:
        body = ";".join(f"{key}={fmt(val)}" for key, val in params.items())
        parts.append(body)
    return ("|".join(parts) + "\n").encode("utf-8")


def encode_response(req_id: int, ok: bool, **params: object) -> bytes:
    parts = [str(req_id), f"ok={1 if ok else 0}"]
    if params:
        parts.append(";".join(f"{key}={fmt(val)}" for key, val in params.items()))
    return ("|".join(parts) + "\n").encode("utf-8")


def parse_response(line: str) -> tuple[int, dict[str, str]]:
    """Return (id, params). ``ok`` dinormalisasi ke '1'/'0' di params."""
    text = line.strip()
    if not text:
        raise WireError("baris kosong")
    parts = text.split("|")
    if len(parts) not in (2, 3):
        raise WireError(f"butuh 2 atau 3 bagian: {text!r}")
    try:
        req_id = int(parts[0])
    except ValueError as exc:
        raise WireError(f"id bukan angka: {parts[0]!r}") from exc
    if req_id <= 0:
        raise WireError(f"id harus > 0: {req_id}")
    key, sep, ok = parts[1].partition("=")
    if key != "ok" or not sep or ok not in ("0", "1"):
        raise WireError(f"field ok hilang/tidak valid: {text!r}")
    params: dict[str, str] = {"ok": ok}
    if len(parts) == 3 and parts[2]:
        for segment in parts[2].split(";"):
            if not segment:
                raise WireError(f"segmen kosong: {text!r}")
            pkey, psep, value = segment.partition("=")
            if not psep or not pkey:
                raise WireError(f"segmen tanpa key=value: {segment!r}")
            params[pkey] = value
    return req_id, params


def parse_request(line: str) -> tuple[int, str, dict[str, str]]:
    """Kebalikan `encode_request` — dipakai simulator EA dan pengujian."""
    text = line.strip()
    if not text:
        raise WireError("baris kosong")
    parts = text.split("|")
    if len(parts) < 2:
        raise WireError(f"butuh minimal 2 bagian: {text!r}")
    try:
        req_id = int(parts[0])
    except ValueError as exc:
        raise WireError(f"id bukan angka: {parts[0]!r}") from exc
    cmd = parts[1]
    if not _CMD_RE.match(cmd):
        raise WireError(f"cmd tidak valid: {cmd!r}")
    params: dict[str, str] = {}
    if len(parts) >= 3 and parts[2]:
        for segment in parts[2].split(";"):
            if not segment:
                continue
            key, sep, value = segment.partition("=")
            if not sep or not key:
                raise WireError(f"segmen tanpa key=value: {segment!r}")
            params[key] = value
    return req_id, cmd, params


def parse_values(params: Mapping[str, str]) -> dict[str, str]:
    """Hook sempit: satu tempat kalau nanti butuh decoding (mis. unescape)."""
    return dict(params)


def ok_of(params: Mapping[str, str]) -> bool:
    return params.get("ok") == "1"


def as_int(params: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = params.get(key)
    if raw is None or raw == "":
        if default is None:
            raise WireError(f"field {key!r} wajib ada")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise WireError(f"field {key!r} bukan integer: {raw!r}") from exc


def as_float(params: Mapping[str, str], key: str, default: float | None = None) -> float:
    raw = params.get(key)
    if raw is None or raw == "":
        if default is None:
            raise WireError(f"field {key!r} wajib ada")
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise WireError(f"field {key!r} bukan angka: {raw!r}") from exc
