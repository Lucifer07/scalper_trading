"""Entrypoint monitor: load .env → logging → runtime loop.

Semua orkestrasi ada di `runner.MonitorRunner`; file ini sengaja tipis supaya
`python -m xau_monitor.main` tetap kontrak stabil (systemd, panel, tools).
"""
from __future__ import annotations

import logging
import sys

from .config import load_settings
from .logsetup import configure
from .runner import MonitorRunner


def main() -> int:
    settings = load_settings()
    configure(settings.panel.log_file, settings.panel.log_max_bytes,
              settings.panel.log_backups)
    log = logging.getLogger("main")
    log.info("xau-monitor start · symbol=%s · panel control=%s:%d",
             settings.symbol, settings.panel.control_host, settings.panel.control_port)
    return MonitorRunner(settings).run()


if __name__ == "__main__":
    sys.exit(main())
