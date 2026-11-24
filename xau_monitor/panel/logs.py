"""Tail file log inkremental: O(delta) per polling, aman untuk rotasi.

Gunakan cursor byte offset yang dikembalikan server. Cursor hilang/lebih besar
dari ukuran file (log dirotasi) => mulai lagi dari ekor file.
"""
from __future__ import annotations

from pathlib import Path

TAIL_BYTES = 200_000
MAX_CHUNK = 256_000
MAX_LINES = 600


class LogTail:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self, cursor: int | None = None, limit: int = 300) -> dict:
        limit = max(1, min(int(limit), MAX_LINES))
        if not self.path.exists():
            return {"lines": [], "cursor": 0, "size": 0, "reset": False,
                    "missing": True, "path": str(self.path)}
        size = self.path.stat().st_size
        start, reset = self._start(cursor, size)
        if start >= size:
            return {"lines": [], "cursor": size, "size": size, "reset": reset,
                    "missing": False, "path": str(self.path)}

        with self.path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read(MAX_CHUNK)
        end = start + len(chunk)
        if chunk and not chunk.endswith(b"\n"):
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                chunk = chunk[:cut + 1]
                end = start + len(chunk)
            else:                       # hanya potongan baris — tunggu newline
                chunk, end = b"", start
        lines = chunk.decode("utf-8", "replace").splitlines()
        if reset and start > 0 and lines:
            lines = lines[1:]          # baris pertama bisa parsial (mulai di tengah baris)
        return {"lines": lines[-limit:], "cursor": end, "size": size, "reset": reset,
                "missing": False, "path": str(self.path)}

    @staticmethod
    def _start(cursor: int | None, size: int) -> tuple[int, bool]:
        if cursor is None or cursor <= 0 or cursor > size:
            return max(0, size - TAIL_BYTES), True
        return int(cursor), False

    def download_path(self) -> Path | None:
        return self.path if self.path.exists() else None
