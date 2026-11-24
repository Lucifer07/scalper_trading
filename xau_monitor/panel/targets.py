"""Registry instance monitor untuk panel multi-simbol: simbol -> (control, log).

Satu panel di satu port melayani banyak instance monitor lewat parameter
`instance`. Sumber kebenaran alokasi port ada di unit systemd; file
`symbols.json` hanya memetakan simbol -> control port + file log.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .control_client import ControlClient
from .logs import LogTail

log = logging.getLogger("panel")


@dataclass(frozen=True)
class Target:
    """Satu instance monitor: klien control API + tail log simbolnya."""

    symbol: str
    control: ControlClient
    log: LogTail


def load_targets(root: Path, registry_path: Path | None = None) -> dict[str, Target]:
    """Baca registry. File absen -> {} (panel jatuh ke mode simbol tunggal).

    File rusak / kosong -> SystemExit: gagal keras, jangan diam-diam
    menampilkan instance yang salah.
    """
    path = registry_path or (root / "symbols.json")
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        targets: dict[str, Target] = {}
        for symbol, cfg in raw.items():
            sym = str(symbol).strip().upper()
            port = int(cfg["control_port"])
            log_path = Path(cfg.get("log", f"logs/{sym.lower()}.log")).expanduser()
            if not log_path.is_absolute():
                log_path = root / log_path
            host = str(cfg.get("control_host", "127.0.0.1"))
            targets[sym] = Target(sym, ControlClient(host, port), LogTail(log_path))
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"[panel] FATAL: {path} tidak valid: {exc}") from None
    if not targets:
        raise SystemExit(f"[panel] FATAL: {path} kosong — isi atau hapus file.")
    log.info("registry %d instance: %s", len(targets), ", ".join(targets))
    return targets
