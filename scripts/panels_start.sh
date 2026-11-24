#!/usr/bin/env bash
# Panel GUI terpadu: SATU port (8773) untuk semua simbol.
# Daftar instance dibaca dari symbols.json; pergantian via dropdown di UI.
set -euo pipefail
cd /home/jayz/xau-monitor
nohup .venv/bin/python -m xau_monitor.panel > /tmp/opencode/panel_unified.log 2>&1 &
echo "panel terpadu PID $! -> http://127.0.0.1:8773"
