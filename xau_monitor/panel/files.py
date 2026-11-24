"""Registry file yang boleh dibaca/diubah panel — nama file tidak dari klien.

Keamanan:
- id file berasal dari whitelist (`env`, `env_example`, `md:<relpath>`, `rules`),
  bukan path bebas dari browser.
- path hasil resolve wajib di dalam root repo, kecuali `RULES_MD` yang memang
  berada di luar repo dan hanya bisa dicapai lewat id `rules`.
- `.env` dibaca dengan nilai rahasia disensor (`__KEEP_SECRET__`). Saat disimpan,
  baris yang masih bertanda sensor dipulihkan dari file lama — token tidak
  pernah dikirim ke browser dan tidak tertimpa saat user hanya mengedit baris lain.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)", re.I)
KEEP = "__KEEP_SECRET__"
MAX_EDIT_BYTES = 512 * 1024
BACKUP_DIR = ".backups"


class FileError(Exception):
    code = 400


class UnknownFile(FileError):
    code = 404


class FileConflict(FileError):
    code = 409


class FileTooLarge(FileError):
    code = 413


@dataclass(frozen=True)
class EditableFile:
    id: str
    label: str
    path: Path
    secret: bool = False


class FileRegistry:
    def __init__(self, root: Path, rules_path: str = "") -> None:
        self.root = Path(root).resolve()
        self._files = self._build(Path(rules_path).expanduser() if rules_path else None)

    # ---- registry --------------------------------------------------
    def _build(self, rules: Path | None) -> dict[str, EditableFile]:
        files = {
            "env": EditableFile("env", ".env", self.root / ".env", secret=True),
            "env_example": EditableFile("env_example", ".env.example",
                                        self.root / ".env.example"),
            "readme": EditableFile("readme", "README.md", self.root / "README.md"),
        }
        if rules:
            files["rules"] = EditableFile("rules", "Aturan AI (.md)", rules)
        for path in sorted(self.root.glob("*.md")):
            if path.name == "README.md":
                continue
            files[f"md:{path.name}"] = EditableFile(f"md:{path.name}", path.name, path)
        for path in sorted((self.root / "docs").glob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            files[f"md:{rel}"] = EditableFile(f"md:{rel}", rel, path)
        return files

    def get(self, file_id: str) -> EditableFile:
        try:
            item = self._files[file_id]
        except KeyError:
            raise UnknownFile(f"file tidak dikenal: {file_id!r}") from None
        return item

    def list(self) -> list[dict]:
        out = []
        for item in self._files.values():
            exists = item.path.exists()
            stat = item.path.stat() if exists else None
            out.append({
                "id": item.id,
                "label": item.label,
                "path": self._display_path(item.path),
                "exists": exists,
                "secret": item.secret,
                "size": stat.st_size if stat else 0,
                "mtime": round(stat.st_mtime, 3) if stat else 0.0,
            })
        out.sort(key=lambda f: (f["id"] not in ("env", "rules"), f["label"].lower()))
        return out

    # ---- baca/tulis -------------------------------------------------
    def read(self, file_id: str) -> dict:
        item = self.get(file_id)
        if not item.path.exists():
            return {"id": item.id, "label": item.label, "path": self._display_path(item.path),
                    "exists": False, "content": "", "mtime": 0.0, "mtime_ns": 0,
                    "size": 0, "secret": item.secret}
        raw = item.path.read_text(encoding="utf-8", errors="replace")
        if len(raw.encode("utf-8")) > MAX_EDIT_BYTES:
            raise FileTooLarge(f"{item.label} terlalu besar (> {MAX_EDIT_BYTES} byte)")
        stat = item.path.stat()
        return {
            "id": item.id, "label": item.label, "path": self._display_path(item.path),
            "exists": True, "content": _mask_env(raw) if item.secret else raw,
            "mtime": round(stat.st_mtime, 3), "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size, "secret": item.secret,
        }

    def write(self, file_id: str, content: str, mtime_ns: int | None = None,
              force: bool = False) -> dict:
        item = self.get(file_id)
        if not isinstance(content, str):
            raise FileError("content harus string")
        payload = content.encode("utf-8")
        if len(payload) > MAX_EDIT_BYTES:
            raise FileTooLarge(f"{item.label} terlalu besar (> {MAX_EDIT_BYTES} byte)")

        current = item.path.read_text(encoding="utf-8", errors="replace") \
            if item.path.exists() else ""
        if item.secret:
            content = _restore_env(content, current)
        if not force and mtime_ns is not None and item.path.exists():
            if item.path.stat().st_mtime_ns != int(mtime_ns):
                raise FileConflict("file berubah di luar panel — muat ulang sebelum menyimpan")

        backup = self._backup(item, content, current)
        _atomic_write(item.path, content.encode("utf-8"), 0o600 if item.secret else 0o644)
        stat = item.path.stat()
        return {"id": item.id, "ok": True, "mtime": round(stat.st_mtime, 3),
                "mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "backup": backup,
                "message": "tersimpan" + (" (backup dibuat)" if backup else "")}

    # ---- util ------------------------------------------------------
    def _backup(self, item: EditableFile, new: str, current: str) -> str:
        if not item.path.exists() or current == new:
            return ""
        target = self.root / BACKUP_DIR
        target.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = target / f"{item.path.name}.{stamp}.bak"
        counter = 1
        while dest.exists():
            dest = target / f"{item.path.name}.{stamp}-{counter}.bak"
            counter += 1
        shutil.copy2(item.path, dest)
        return dest.relative_to(self.root).as_posix()

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)


def _mask_env(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        key, _ = _env_pair(line)
        if key is None or not SECRET_KEY.search(key):
            out.append(line)
            continue
        ending = line[len(line.rstrip("\r\n")):]
        raw = line.rstrip("\r\n")
        eq = raw.find("=")
        out.append(f"{raw[:eq]}={KEEP}{ending}")
    return "".join(out)


def _restore_env(text: str, original: str) -> str:
    """Pulihkan baris rahasia yang masih bertanda `__KEEP_SECRET__`."""
    stored: dict[str, str] = {}
    for line in original.splitlines(keepends=True):
        key, value = _env_pair(line)
        if key is not None and SECRET_KEY.search(key):
            stored[key] = line
    out = []
    for line in text.splitlines(keepends=True):
        key, value = _env_pair(line)
        if key is not None and value == KEEP and key in stored:
            out.append(stored[key] if stored[key].endswith(("\n", "\r")) else stored[key] + "\n")
            continue
        out.append(line)
    return "".join(out)


def _env_pair(line: str) -> tuple[str | None, str | None]:
    raw = line.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        return None, None
    key, _, value = raw.partition("=")
    return key.strip(), value.strip().strip('"').strip("'")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
