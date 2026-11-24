#!/usr/bin/env bash
# Matikan seluruh stack xau-monitor setelah market tutup.
# Dipanggil cron Sabtu 05:15 WIB (market XAUUSD/FX tutup ~Sabtu 05:00 WIB broker GMT+2).
# Stop PARALEL per fase (satu panggilan systemctl per grup — bukan per unit):
#   fase 1: 5 monitor   (systemd tunggu graceful maks TimeoutStopSec=90s/unit)
#   fase 2: terminal MT4 + panel
# Monitor yang sedang fase seed startup tidak terputus SIGTERM sampai seed selesai
# (maks 45s) — itulah kenapa stop harus paralel, tidak berurutan.
set -u
cd "$(dirname "$0")/.."
LOG=logs/cron-market-hours.log
mkdir -p logs
exec >> "$LOG" 2>&1
echo "== $(date '+%F %T') MARKET CLOSE — mematikan stack =="
if systemctl --user stop \
    xau-monitor.service xau-monitor-eurusd.service xau-monitor-gbpusd.service \
    xau-monitor-us100.service xau-monitor-xrbusd.service; then
  echo "$(date '+%F %T') STOP 5 unit monitor OK (paralel)"
else
  echo "$(date '+%F %T') STOP monitor GAGAL sebagian — status:"
  systemctl --user list-units --all --no-pager | grep -iE 'xau-monitor' || true
fi
if systemctl --user stop mt4-fbs.service xau-panel.service; then
  echo "$(date '+%F %T') STOP mt4-fbs + xau-panel OK"
else
  echo "$(date '+%F %T') STOP mt4-fbs/xau-panel GAGAL — status:"
  systemctl --user list-units --all --no-pager | grep -iE 'mt4|xau-panel' || true
fi
systemctl --user reset-failed 2>/dev/null || true
echo "== $(date '+%F %T') MARKET CLOSE selesai =="
