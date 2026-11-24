#!/usr/bin/env bash
# Jalankan terminal FBS (MT4/Wine) di display GNOME aktif.
# XAUTHORITY mutter berubah nama tiap login, jadi diresolve dinamis dari
# proses Xwayland. Dipakai oleh unit systemd user mt4-fbs.service.
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-$HOME/.mt4}"
export WINEARCH="${WINEARCH:-win64}"
export DISPLAY="${DISPLAY:-:0}"

for _ in $(seq 1 30); do
  XW_PID=$(pgrep -x Xwayland | head -1 || true)
  [ -n "$XW_PID" ] && break
  sleep 2
done
if [ -z "${XW_PID:-}" ]; then
  echo "Xwayland belum jalan — tidak bisa menampilkan MT4" >&2
  exit 1
fi

AUTH=$(tr '\0' ' ' < "/proc/$XW_PID/cmdline" | grep -oE '\-auth [^ ]+' | awk '{print $2}')
if [ -n "$AUTH" ] && [ -f "$AUTH" ]; then
  export XAUTHORITY="$AUTH"
fi

exec wine "$HOME/.mt4/drive_c/Program Files (x86)/FBS Trader 4/terminal.exe"
