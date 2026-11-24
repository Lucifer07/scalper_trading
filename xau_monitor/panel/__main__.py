"""Entrypoint panel: `python -m xau_monitor.panel`.

Bind wajib loopback: control API dan editor .env tidak punya autentikasi.
Untuk akses dari mesin lain gunakan SSH tunnel:
    ssh -L 8773:127.0.0.1:8773 user@host
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..logsetup import configure
from .control_client import ControlClient
from .files import FileRegistry
from .logs import LogTail
from .server import PanelApp, PanelServer
from .supervisor import Supervisor
from .targets import Target, load_targets

log = logging.getLogger("panel")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):          # stdout pipa/tertutup
        pass
    root = Path(args.root).expanduser().resolve()
    if not (root / "xau_monitor").is_dir():
        print(f"[panel] FATAL: {root} bukan root repo xau-monitor", file=sys.stderr)
        return 2
    if args.host not in LOOPBACK:
        print(f"[panel] FATAL: host {args.host!r} bukan loopback — panel tanpa auth. "
              "Pakai 127.0.0.1 + SSH tunnel.", file=sys.stderr)
        return 2

    dotenv = _read_dotenv(root / ".env")
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    configure(str(log_dir / "panel.log"), 1_000_000, 3)

    control_port = args.control_port or _int(dotenv.get("CONTROL_PORT"), 8772)
    control_host = dotenv.get("CONTROL_HOST", "127.0.0.1")
    log_file = Path(args.log_file or dotenv.get("LOG_FILE", "logs/monitor.log")).expanduser()
    if not log_file.is_absolute():
        log_file = root / log_file
    files = FileRegistry(root, dotenv.get("RULES_MD", ""))

    registry = load_targets(root)
    if registry:
        targets = registry
        default_instance = dotenv.get("SYMBOL", "").strip().upper() \
            or next(iter(registry))
        control = targets[default_instance].control
    else:
        default_instance = dotenv.get("SYMBOL", "MONITOR").strip().upper() or "MONITOR"
        control = ControlClient(control_host, control_port)
        targets = {default_instance: Target(default_instance, control,
                                            LogTail(log_file))}
    supervisor = Supervisor(root, log_dir=log_dir, python=args.python,
                            env_extra={"CONTROL_HOST": control_host,
                                       "CONTROL_PORT": str(control_port)})
    app = PanelApp(supervisor, files, LogTail(supervisor.stdio_log),
                   targets, default_instance)

    control_on = dotenv.get("CONTROL_ENABLED", "1").strip() != "0"
    spawned = _autostart(supervisor, control, args.autostart and control_on)
    try:
        httpd = PanelServer((args.host, args.port), app)
    except OSError as exc:
        print(f"[panel] FATAL: bind {args.host}:{args.port} gagal: {exc}", file=sys.stderr)
        return 2
    _banner(args, control, root, dotenv)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[panel] berhenti")
    finally:
        httpd.server_close()
        if spawned and args.stop_on_exit:
            print("[panel] menghentikan monitor child ...")
            supervisor.stop()
        elif spawned:
            print("[panel] monitor child tetap berjalan (pakai tombol Stop atau --stop-on-exit)")
    return 0


def _autostart(supervisor: Supervisor, control: ControlClient, enabled: bool) -> bool:
    """True bila panel yang menyalakan child monitor."""
    if not enabled:
        log.info("autostart dimatikan — panel hanya memantau")
        return False
    if supervisor.alive():
        return False
    payload, err = control.status()
    if not err and payload:
        log.info("monitor eksternal terdeteksi di control API — panel tidak menyalakan child")
        return False
    result = supervisor.start()
    log.info("%s (pid %s)", result["message"], result["monitor"].get("pid"))
    return True


def _banner(args, control: ControlClient, root: Path, dotenv: dict[str, str]) -> None:
    print("─" * 62)
    print("  XAU Monitor — panel GUI")
    print(f"  Buka      : http://{args.host}:{args.port}")
    print(f"  Repo      : {root}")
    print(f"  Control   : {control.base}")
    print(f"  Log       : {dotenv.get('LOG_FILE', 'logs/monitor.log')}")
    print(f"  Aturan AI : {dotenv.get('RULES_MD', '—')}")
    print("  Stop      : Ctrl-C (monitor child tetap hidup kecuali --stop-on-exit)")
    print("─" * 62)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xau_monitor.panel",
                                     description="Panel GUI lokal xau-monitor")
    parser.add_argument("--root", default=".", help="root repo (default: cwd)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (loopback saja)")
    parser.add_argument("--port", type=int, default=8773, help="port panel (default 8773)")
    parser.add_argument("--control-port", type=int, default=0,
                        help="port control API monitor (default: CONTROL_PORT/.env)")
    parser.add_argument("--log-file", default=None,
                        help="file log monitor yang ditampilkan (default: LOG_FILE/.env)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter untuk child monitor")
    parser.add_argument("--no-autostart", dest="autostart", action="store_false",
                        help="jangan nyalakan monitor otomatis")
    parser.add_argument("--stop-on-exit", action="store_true",
                        help="hentikan child monitor saat panel ditutup")
    return parser.parse_args(argv)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


if __name__ == "__main__":
    sys.exit(main())
