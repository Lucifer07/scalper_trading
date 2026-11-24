#!/usr/bin/env bash
# Nyalakan seluruh stack xau-monitor saat open market.
# Dipanggil cron Senin 10:00 WIB (jam mulai sesi).
# Urutan: terminal MT4 dulu (boot wine + EA attach + dial bridge +-45 dtk),
# lalu 5 monitor + panel SEKALIGUS (start tidak blocking; monitor yang
# keburu jalan sebelum EA siap akan retry sendiri via Restart=on-failure).
set -u
cd "$(dirname "$0")/.."
LOG=logs/cron-market-hours.log
mkdir -p logs
exec >> "$LOG" 2>&1
echo "== $(date '+%F %T') MARKET OPEN — menyalakan stack =="
if systemctl --user start mt4-fbs.service; then
  echo "$(date '+%F %T') START mt4-fbs.service OK"
else
  echo "$(date '+%F %T') START mt4-fbs.service GAGAL"
fi
echo "$(date '+%F %T') tunggu 45s boot MT4 + EA dial bridge ..."
sleep 45
if systemctl --user start \
    xau-monitor.service xau-monitor-eurusd.service xau-monitor-gbpusd.service \
    xau-monitor-us100.service xau-monitor-xrbusd.service xau-panel.service; then
  echo "$(date '+%F %T') START 5 monitor + panel OK (paralel)"
else
  echo "$(date '+%F %T') START sebagian GAGAL — status:"
  systemctl --user list-units --all --no-pager | grep -iE 'xau|mt4' || true
fi
echo "== $(date '+%F %T') MARKET OPEN selesai =="
