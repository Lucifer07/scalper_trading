# Panel GUI — xau-monitor

Panel lokal (browser) untuk mengoperasikan monitor tanpa menyentuh shell:
pindah simbol saat runtime, baca log, dan edit file aturan `.md`/`.env`.

```
┌─────────────────────── panel :8773 (proses terpisah) ───────────────────────┐
│  Supervisor ──start/stop/restart──► python -m xau_monitor.main              │
│  ControlClient ──GET/POST :8772──► control API di dalam monitor              │
│  LogTail ───────tail────────────► logs/monitor.log (rotating)               │
│  FileRegistry ──read/write──────► .env, *.md, docs/*.md                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Jalankan

```bash
cd ~/xau-monitor
make gui          # = .venv/bin/python -m xau_monitor.panel
# buka http://127.0.0.1:8773
# SATU panel untuk semua simbol — daftar instance di symbols.json,
# pergantian simbol lewat dropdown di sidebar (parameter `instance`).
```

Panel otomatis menyalakan monitor (`python -m xau_monitor.main`) bila control
API belum aktif. Bila monitor sudah jalan (systemd / terminal lain), panel hanya
memantau dan tombol Start/Stop dinonaktifkan.

Opsi:

| Flag | Arti |
| --- | --- |
| `--root PATH` | root repo (default: cwd) |
| `--host 127.0.0.1` | bind host — **wajib loopback** |
| `--port 8773` | port panel |
| `--control-port 8772` | port control API (default: `CONTROL_PORT` di `.env`) |
| `--no-autostart` | jangan nyalakan monitor otomatis |
| `--stop-on-exit` | hentikan child monitor saat panel ditutup |

`Ctrl-C` menghentikan panel saja; monitor child tetap berjalan kecuali
`--stop-on-exit`. Ini disengaja: menutup UI tidak boleh mematikan pemantau
yang sedang mengirim sinyal.

## Akses dari mesin lain

Panel tidak punya autentikasi (hanya bind loopback). Jangan membuka
`--host 0.0.0.0`. Gunakan SSH tunnel:

```bash
ssh -L 8773:127.0.0.1:8773 user@server-xau
# lalu buka http://127.0.0.1:8773 di laptop
```

## Menu

### 1. Dashboard

- Harga live + sparkline 240 sampel terakhir (dari polling status).
- **Kesiapan trigger** per mode: persen gate lolos (kurang berapa persen),
  harga target entry, jarak %, posisi ekstrem (D1 sniper / M15 handgun) vs
  ambang ≤20% / ≥80%, level pantau terdekat, dan blocker gate berikutnya.
- Rate tick, jumlah tick MT4, umur tick MT4, status EA bridge.
- State engine per mode (`sniper`, `handgun`), blocker, status order, broker.
- Laporan **Planning** / **Status** — teks yang sama dengan `/planning` dan
  `/status` di bot Telegram (tag HTML di-strip).
- Tombol Start / Restart / Stop untuk child monitor.

### 2. Simbol

Pindah simbol runtime tanpa restart (`POST /api/symbol`): state engine di-reset,
history di-seed ulang dari broker, ditolak bila masih ada posisi terbuka atau
simbol tidak dikenal terminal.

Sumber tick **tidak bisa dipilih lagi dari panel**: satu-satunya sumber adalah
**MT4** (bar M1 native broker lewat EA bridge). Feed kantor / FIX worker sudah
dipensiunkan. Bila tick MT4 mandek, watchdog (`alerts.py`) yang memberi tahu
lewat Telegram — tidak ada fallback ke sumber lain.

### 3. Log

Tail `logs/monitor.log` (rotating, default 2 MB × 5) dan
`logs/monitor.stdio.log` (stdout/stderr child — traceback crash). Fitur:
ikuti otomatis, filter teks, unduh file. Cursor byte membuat polling hanya
membaca bagian baru — biaya `O(delta)`, bukan `O(file)`.

### 4. File & Aturan AI

Editor untuk:

- `.env` — rahasia (token/key/password) **disensor** sebagai `__KEEP_SECRET__`.
  Biarkan baris itu apa adanya saat menyimpan perubahan lain; nilainya
  dipulihkan dari file lama di server. Token tidak pernah dikirim ke browser.
- `RULES_MD` (mis. `~/ringkasan-scalping-xau-coach-tom.md`) — aturan Coach Tom
  yang dibaca advisor AI.
- `README.md`, `*.md` di root, `docs/*.md`.

Setiap penyimpanan menulis file secara atomik (`os.replace`), membuat backup di
`.backups/<nama>.<timestamp>.bak`, dan menolak menyimpan bila file berubah di
luar panel — kecuali Anda mencentang **timpa paksa**.

## API

Control API monitor (`:8772`, loopback):

| Method | Path | Fungsi |
| --- | --- | --- |
| GET | `/api/health` | hidup? |
| GET | `/api/status` | JSON status (harga, tick, engine, order, broker, kesiapan trigger) |
| GET | `/api/report?kind=status\|planning\|help` | teks laporan Telegram |
| POST | `/api/symbol` `{"symbol":"EURUSD"}` | pindah simbol runtime: state di-reset, seed ulang; ditolak bila ada posisi terbuka / simbol tak dikenal |

Panel (`:8773`, loopback):

| Method | Path | Fungsi |
| --- | --- | --- |
| GET | `/api/state?instance=SYM` | supervisor + status monitor instance itu |
| GET | `/api/symbols` | daftar instance terdaftar (symbols.json) + default |
| POST | `/api/monitor/start\|stop\|restart` | kontrol child process |
| POST | `/api/symbol` | proxy pindah simbol runtime |
| GET | `/api/report?kind=` | proxy laporan |
| GET | `/api/logs?source=&cursor=&limit=` | tail inkremental |
| GET | `/api/logs/download?source=` | unduh log |
| GET | `/api/files`, `/api/file?id=` | daftar / baca file |
| PUT | `/api/file` | simpan `{id, content, mtime, force}` |

Mutasi wajib menyertakan header `X-Xau-Panel: 1` (control API:
`X-Xau-Control: 1`) dan `Content-Type: application/json`. Tanpa CORS, halaman
web asing tidak bisa memicu permintaan tulis ke localhost (CSRF). Selain itu
`Host` header wajib nama loopback (`127.0.0.1`/`localhost`) untuk menutup
DNS rebinding, dan server menolak klien non-loopback.

Loop monitor yang sedang berjalan memuat modul Python saat start; setelah
memperbarui kode, restart monitor (`systemctl --user restart xau-monitor`)
agar control API versi baru terpakai. Panel sendiri cukup di-refresh.

## Konfigurasi terkait

```dotenv
CONTROL_ENABLED=1        # 0 = matikan control API (panel hanya baca log; autostart dimatikan)
CONTROL_HOST=127.0.0.1   # wajib loopback
CONTROL_PORT=8772        # beda dengan MT4_BRIDGE_PORT
LOG_FILE=logs/monitor.log
LOG_MAX_BYTES=2000000
LOG_BACKUPS=5
```

`HISTORY_SOURCE` menentukan sumber seed TF besar; tick selalu dari MT4
(`MT4_TICK_POLL_MS` mengatur frekuensi polling bar M1). Panel mengubah simbol
**runtime** tanpa mengubah `.env`. Bila ingin perubahan permanen, edit `.env`
di menu File lalu Restart.

## Batasan

- Satu monitor per host per `CONTROL_PORT` (bind eksklusif).
- Panel tidak menyimpan histori harga; sparkline hanya data sejak tab dibuka.
- Panel tidak menggantikan systemd untuk produksi 24/7 — unit systemd tetap
  cara yang benar untuk auto-restart.
