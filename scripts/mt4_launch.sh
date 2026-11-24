#!/usr/bin/env bash
# Salin EA bridge ke SEMUA MQL4/Experts (install dir + data folder tiap terminal),
# kompilasi via metaeditor CLI, lalu jalankan MT4.
# Catatan: MT4 modern memakai data folder di
#   %APPDATA%\MetaQuotes\Terminal\<hash>\MQL4\Experts
# sehingga menyalin hanya ke folder instalasi membuat EA "hilang" setelah
# terminal restart / ganti terminal. Script ini mengiterasi semuanya.
# Pakai: ./scripts/mt4_launch.sh [--install-only]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt4}"
export WINEARCH="${WINEARCH:-win64}"
export DISPLAY="${DISPLAY:-:0}"

term="$(find "$WINEPREFIX/drive_c" -maxdepth 6 -iname 'terminal.exe' -print -quit 2>/dev/null || true)"
editor="$(find "$WINEPREFIX/drive_c" -maxdepth 6 -iname 'metaeditor.exe' -print -quit 2>/dev/null || true)"

mapfile -t experts_dirs < <(find "$WINEPREFIX/drive_c" -maxdepth 12 -type d \
  -name Experts -path '*MQL4*' 2>/dev/null | sort)
if (( ${#experts_dirs[@]} == 0 )); then
  echo "[mt4] MQL4/Experts belum ada di $WINEPREFIX" >&2
  echo "[mt4] install MT4 dulu, lalu buka terminal sekali agar data folder terbentuk." >&2
  exit 1
fi

# Path wine -> Windows untuk metaeditor CLI
win_path() { printf 'C:%s' "${1#"$WINEPREFIX/drive_c"}" | sed 's#/#\\#g'; }

copy_ea() {
  local dir="$1"
  cp "$ROOT/ea/XauBridge.mq4" "$ROOT/ea/XauBridgeCommands.mqh" "$dir/"
  echo "[mt4] EA disalin ke $dir"
}

compile_ea() {
  local dir="$1" src log result
  src="$(win_path "$dir/XauBridge.mq4")"
  log="$(win_path "$dir/compile.log")"
  rm -f "$dir/compile.log"
  WINEPREFIX="$WINEPREFIX" wine "$editor" /compile:"$src" /log:"$log" >/dev/null 2>&1 || true
  if [[ ! -f "$dir/compile.log" ]]; then
    echo "[mt4] peringatan: log kompilasi tidak terbentuk di $dir" >&2
    return 1
  fi
  result="$(iconv -f UTF-16LE -t UTF-8 "$dir/compile.log" 2>/dev/null \
    | tr -d '\000' | grep -i '^Result:' | tail -1)"
  echo "[mt4] kompilasi ${dir#"$WINEPREFIX/drive_c"}: ${result:-tidak ada hasil}"
  [[ "$result" == *"0 errors"* ]] || { echo "[mt4] KOMPILASI GAGAL — cek $dir/compile.log" >&2; return 1; }
}

failed=0
for dir in "${experts_dirs[@]}"; do
  copy_ea "$dir"
  if [[ -n "$editor" ]]; then
    compile_ea "$dir" || failed=1
  fi
done

# Konfigurasi per-simbol untuk EA: MQL4/Files/xaubridge.ini (satu sumber:
# symbols.json). Ini otoritatif atas input chart, karena MT4 kerap me-reset
# input chart ke default (port 8770) sehingga semua EA menumpuk di satu port.
write_ea_ini() {
  local experts="$1" ini="${1%/Experts}/Files/xaubridge.ini"
  mkdir -p "$(dirname "$ini")"
  python3 - "$ROOT/symbols.json" "$ini" <<'PY'
import json
import sys

src, out = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    data = json.load(fh)
lines = [
    "# xau-monitor: otorisasi per-simbol (dibuat otomatis dari symbols.json).",
    "# format: SIMBOL=bridge_port,magic,allow(0/1). Input chart = fallback.",
]
for sym, cfg in sorted(data.items()):
    if "bridge_port" not in cfg:
        continue
    lines.append(f"{sym}={cfg['bridge_port']},{cfg['magic']},{int(cfg.get('ea_allow_trading', 0))}")
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"[mt4] xaubridge.ini -> {out}")
PY
}

for dir in "${experts_dirs[@]}"; do
  write_ea_ini "$dir"
done

if (( failed != 0 )); then
  echo "[mt4] ada folder yang gagal dikompilasi — perbaiki dulu sebelum trading." >&2
  exit 2
fi

echo "[mt4] EA siap di ${#experts_dirs[@]} folder Experts"
echo "[mt4] di terminal: Tools→Options→Expert Advisors (Allow DLL imports + automated trading),"
echo "[mt4] Navigator→Expert Advisors→Refresh, lalu drag XauBridge ke chart XAUUSD M5."
echo "[mt4] Biarkan InpAllowTrading=false untuk mode read-only (tick MT4 tetap jalan)."

if [[ "${1:-}" == "--install-only" ]]; then
  echo "[mt4] terminal.exe: ${term:-<tidak ditemukan>}"
  exit 0
fi
if [[ -z "$term" ]]; then
  echo "[mt4] terminal.exe tidak ditemukan — buka MT4 manual." >&2
  exit 1
fi
exec wine "$term"
