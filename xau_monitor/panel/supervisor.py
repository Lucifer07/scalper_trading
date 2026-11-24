"""Supervisor child process monitor untuk panel GUI.

Monitor tetap proses terpisah (kontrak CLI/systemd tidak berubah). Panel hanya
menyalakan/mematikan `python -m xau_monitor.main` dan melaporkan state. Log
monitor ditulis sendiri oleh `logging` ke `logs/monitor.log`; stdout/stderr
child diarahkan ke `logs/monitor.stdio.log` supaya traceback crash tidak hilang
dan pipe tidak pernah penuh.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

STOP_TIMEOUT_S = 15.0


class Supervisor:
    def __init__(self, root: Path, log_dir: Path | None = None,
                 python: str | None = None, module: str = "xau_monitor.main",
                 env_extra: dict[str, str] | None = None) -> None:
        self.root = Path(root).resolve()
        self.python = python or sys.executable
        self.module = module
        self.env_extra = dict(env_extra or {})
        self.log_dir = Path(log_dir) if log_dir else self.root / "logs"
        self.stdio_log = self.log_dir / "monitor.stdio.log"
        self._proc: subprocess.Popen | None = None
        self._started_at = 0.0
        self._last_exit: tuple[int, float] | None = None
        self._lock = threading.RLock()

    # ---- state -----------------------------------------------------
    def alive(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def state(self) -> dict:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._last_exit = (proc.returncode, time.time())
                self._proc = None
                proc = None
            last_exit = self._last_exit
            started = self._started_at
        return {
            "running": proc is not None,
            "pid": proc.pid if proc else None,
            "since": round(started, 1) if proc else None,
            "returncode": last_exit[0] if last_exit else None,
            "exited_at": round(last_exit[1], 1) if last_exit else None,
            "stdio_log": str(self.stdio_log),
        }

    # ---- aksi ------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": True, "message": "monitor sudah berjalan",
                        "monitor": self.state()}
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stdio = self.stdio_log.open("ab", buffering=0)
            try:
                self._proc = subprocess.Popen(
                    [self.python, "-m", self.module],
                    cwd=str(self.root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=stdio,
                    env=self._env())
                self._started_at = time.time()
                self._last_exit = None
            finally:
                stdio.close()
        return {"ok": True, "message": "monitor dimulai", "monitor": self.state()}

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return {"ok": True, "message": "monitor tidak berjalan",
                    "monitor": self.state()}
        proc.terminate()
        try:
            code = proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=5)
        with self._lock:
            self._last_exit = (code, time.time())
            self._proc = None
        return {"ok": True, "message": f"monitor berhenti (exit {code})",
                "monitor": self.state()}

    def restart(self) -> dict:
        stopped = self.stop()
        started = self.start()
        started["message"] = f"{stopped['message']} → {started['message']}"
        return started

    def _env(self) -> dict[str, str]:
        """Env dipakai apa adanya; .env dibaca monitor sendiri dari cwd repo."""
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.update(self.env_extra)
        return env
