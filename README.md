# xau-monitor — Realtime XAUUSD Candle Monitor + Telegram Alerts

Monitor price-action XAUUSD **realtime dari MT4** (bar M1 native broker lewat
EA bridge — satu-satunya sumber tick; feed kantor/FIX worker sudah
dipensiunkan) dan mengirim keputusan **SELL/BUY** ke Telegram menggunakan
rule engine framework candle Coach Tom (naked chart, tanpa indikator).

> ⚠️ **Disclaimer:** alat edukasi/monitoring. Bukan saran finansial. Trigger
> adalah aturan mekanis atas struktur candle — tetap verifikasi manual
> sebelum entry.

## Arsitektur

```
MT4 EA bridge (bar M1 native, poll 500 ms)  Yahoo (GC=F, konteks TF besar)
         │ high/low/close bar berjalan            │ seed per jam (opsional)
         ▼                                         ▼
   Mt4BarTicks ──► M1Builder ──► aggregate/resample ──► TF snapshots
   (thread)         (M1)          M5 M15 M30 H1 H4 D1 W1      │
                                                             ▼
                                         Engine (state machine Coach Tom)
                                         Gate1 seirama → Gate2 turun-dalam
                                         → Gate3 zona resis/sup → Gate4 jarum M5
                                                             │ Signal
                                                             ▼
                                                   TelegramNotifier / console
```

- **Tick hanya dari MT4.** Tidak ada lagi feed kantor/FIX worker dan tidak ada
  fallback sumber lain — bila tick mandek, watchdog memberi tahu lewat
  Telegram dan sinyal tertahan sampai EA bridge pulih.
- **Engine = state machine eksplisit** (`WAIT → SETUP → SENT → COOLDOWN`),
  anti-spam + deteksi invalidasi bias.
- **Candle hanya diproses saat sudah close** (no repaint).
- **Panel GUI** (`make gui`) adalah proses terpisah: control API loopback
  `127.0.0.1:8772` di dalam monitor + UI `127.0.0.1:8773`. Simbol bisa
  diganti saat runtime tanpa restart.

## Setup

```bash
cd ~/xau-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test         # pytest — wajib hijau sebelum run
cp .env.example .env   # isi TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID
make run          # DRY-RUN jika Telegram belum diisi
```

### Panel GUI (opsional, disarankan)

```bash
make gui          # http://127.0.0.1:8773
```

Panel browser lokal untuk: **pindah simbol** tanpa restart, **baca log**
monitor, **edit `RULES_MD` / `.env` / `.md`**, lihat harga + state engine +
laporan `/planning`, dan start/stop/restart monitor.
Detail: **[docs/gui.md](docs/gui.md)**.

### Membuat bot Telegram

1. Chat **@BotFather** → `/newbot` → salin token → `TELEGRAM_BOT_TOKEN`.
2. Kirim pesan apa pun ke bot Anda, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → salin `chat.id` →
   `TELEGRAM_CHAT_ID`.
3. (Grup) Tambahkan bot ke grup, kirim pesan di grup, ambil `chat.id` negatif.

## Perintah bot (kirim ke chat Telegram)

| Perintah | Fungsi |
| --- | --- |
| `/status` | Harga live, state engine, tick rate, uptime, blocker |
| `/planning` | Status keempat gate + alasan belum trigger + rencana entry/CL/TP |
| `/help` | Ringkasan command |

`/planning` menampilkan kondisi tiap gate secara real time, contoh:

```
🧭 XAUUSD — PLANNING
Harga 3887.15 · state WAIT
❌ Gate 1 · TF seirama — W1 DOWN · D1 DOWN · H4 FLAT
✅ Gate 2 · ekstrem D1 (pos 8.0%, butuh ≤20%)
❌ Gate 3 · zona — terdekat 3889.30 (jarak 2.15, toleransi 5.00)
❌ Gate 4 · trigger M5 — wick 12%/45% (butuh wick atas ≥55% + close bearish)
🎯 SNIPER — kesiapan 43% · kurang 57% · 3/7 gate
   target 3889.30 (+0.05% · naik) · berikutnya: zona resis/sup
🎯 Rencana bila semua lolos: SELL zona 3889.30 · CL 3896.4 · TP ...
```

## Perintah CLI

| Perintah | Fungsi |
| --- | --- |
| `make test` | Unit test (candles + strategy + commands + order path) |
| `make run` | Jalankan monitor |
| `make sim` | Simulator EA MT4 (uji jalur order tanpa terminal) |
| `make mcp` | MCP server MT4 read-only (lihat `docs/mt4-integration.md`) |
| `make gui` | Panel GUI lokal `http://127.0.0.1:8773` (lihat `docs/gui.md`) |

## Order otomatis (opsional, default OFF)

Sinyal `ENTRY_*` bisa diteruskan ke MT4 lewat EA bridge: engine → AI advisor
(review/veto + usulan `lots`) → risk guard deterministik (volume di-clamp ke
`lot_min..MAX_LOT`, dicek margin) → **pending limit di level SL engine**
(`sl=0`, `tp=0`), digeser mendekati pasar sebesar
`min(2% × range candle M5 terakhir, 10 pip)` (BUY `+`, SELL `−`).
Pending dibatalkan bila tidak tersentuh dalam
`PENDING_TTL_SECONDS` (default 5 menit). Setelah terisi, posisi dikelola
**trailing TP**: ditutup bila profit retrace ≥ `TRAIL_RETRACE_PCT` (default 20%)
dari puncak profit. Tidak ada reversal (dibatalkan).
**Layering** (opsional, toggle di panel): AI menentukan jumlah layer (min 2)
berdasarkan balance/free margin; semua limit dipasang sekaligus dengan jarak
offset layer pertama, dan jumlahnya dibatasi margin (termasuk floating).
Detail lengkap, protokol, checklist arming, dan batasannya:
**[docs/mt4-integration.md](docs/mt4-integration.md)**.

```bash
make sim      # terminal 1: simulator EA (tanpa MT4)
make run      # terminal 2: monitor; ORDER_ENABLED=0 tetap read-only
```

Tanpa `ORDER_ENABLED=1` + `MT4_ALLOW_TRADING=1` + EA `InpAllowTrading=true`,
tidak ada order yang bisa terkirim. AI advisor default di proyek ini:
`opencode-go/deepseek-v4.1-flash` (tanpa API key, lewat `opencode run`).

## Hard loss stop (opsional, `LOSS_LIMIT_USD`)

Rem darurat akun, bukan per posisi: setiap `LOSS_GUARD_POLL_S` detik monitor
membaca `equity`/`balance` dari EA dan membandingkannya dengan **baseline saldo**
yang dibekukan sekali di `state/loss_guard.json`. Bila
`baseline − equity ≥ LOSS_LIMIT_USD` selama `LOSS_GUARD_CONFIRM` sampel
berurutan, monitor:

1. menulis latch `state/trading_locked.json` (atomik, antar instance),
2. **membatalkan semua pending limit** (kalau tidak, limit bisa terisi setelah
   app mati) lalu **menutup semua posisi** di semua simbol registry,
3. mengirim ringkasan ke Telegram, dan
4. `systemctl --user stop --no-block` semua unit di `LOSS_GUARD_STOP_UNITS`.

Setelah latch ada, monitor yang dijalankan ulang **menolak meng-arm order**
(atau berhenti, bila guard aktif saat start). Membuka kunci = keputusan manual:

```bash
systemctl --user stop 'xau-monitor*' xau-panel.service
rm -f state/trading_locked.json state/loss_guard.json   # baseline baru saat start
systemctl --user start xau-monitor.service
```

`LOSS_LIMIT_USD=0` (default di `.env.example`) mematikan guard sepenuhnya.


## Running 24/7 (systemd user unit)

`~/.config/systemd/user/xau-monitor.service`:

```ini
[Unit]
Description=XAUUSD candle monitor -> Telegram
After=network-online.target

[Service]
WorkingDirectory=%h/xau-monitor
ExecStart=%h/xau-monitor/.venv/bin/python -m xau_monitor.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now xau-monitor
journalctl --user -u xau-monitor -f
```

## Simbol

Ganti `SYMBOL=` di `.env` untuk monitor lain — simbol harus tersedia di
Market Watch MT4 broker Anda (mis. `XAUUSD`, `EURUSD`, `GBPUSD`, `US100`).
Untuk spot gold pakai `XAUUSD` (bukan kontrak `GOLD`/`GoldDec` yang spread-nya
lebar). Perpindahan simbol juga bisa runtime lewat panel tanpa restart.

## Aturan trigger (Coach Tom, sesuai .md)

Struktur aturan mengikuti `.md`, **bukan** semua-AND:

```
mode = sniper | handgun                       (cabang, §6:73-74)
  ├─ prasyarat : naik tinggi → SELL, turun dalam → BUY (diukur di M15, TF entry)
  │              sniper: D1+H4+H1 = ARAH; entry wajib M15 berlawanan arah
  │                      (BUY: M15 ≤20% / SELL: M15 ≥80% = pullback ke zona)
  │              handgun: ekstrem M15 = arah langsung, tidak butuh D1/H4/H1 seirama
  ├─ area      : cluster resis/sup M15 (+M30/H1), toleransi 1×ATR M15 (§2:33, §5:57)
  ├─ pola inti : kocokan di zona + dorongan terakhir (§6:66-67, §8:86)
  │              ATAU jebol-jarum (§7:80-82)        ← OR, bukan AND
  ├─ veto      : pola 1/4 HANYA bila jarum belum tertembus (§7:78-80)
  ├─ modifier  : jebol ganda → target pendek, jangan hold (§7:81-82)
  └─ modifier  : menit terakhir M15 full → tunda; melemah → boleh lawan (§8:88-90)
conviction (semua TF seirama) = syarat HOLD, bukan syarat entry (§11:110)
```

Catatan implementasi:

- **Sniper** tetap butuh D1+H4+H1 seirama (`.md:74`: *"tidak selaras = tidak trading"*)
  sebagai **arah**, tetapi gate entry-nya adalah **ekstrem M15 berlawanan arah**:
  arah UP → wajib M15 `pos20 ≤ 20%` (turun dalam / pullback ke support) untuk BUY;
  arah DOWN → wajib M15 `pos20 ≥ 80%` (naik tinggi / pullback ke resistance) untuk
  SELL. Versi lama memakai ekstrem D1 sehingga BUY bisa terjadi di puncak tanpa
  pullback di TF entry — bertentangan dengan video 21:06–21:42 dan 14:36–15:11.
- **Handgun** mengambil arah **langsung dari ekstrem M15**: `pos20 ≥ 80%` (naik tinggi)
  → SELL, `pos20 ≤ 20%` (turun dalam) → BUY. Versi lama memakai tren M15 sebagai
  arah sehingga membeli di puncak range — 100% sinyal ditolak advisor sebagai
  *chasing*. Lihat verifikasi transkrip video 14:36–15:11, 21:06–21:42, 24:47–25:02.
- Advisor memakai aturan generik per simbol:
  [`docs/rules-price-action-generic.md`](docs/rules-price-action-generic.md)
  (XAUUSD tetap memakai `RULES_MD` dari `.env`). Kalibrasi chasing diukur dari
  `pos20` TF entry (M15/M5), bukan `pos20` D1.
- **Veto pola 1/4 bersifat kondisional**: kalau jarum pucuk sudah ditembus lalu close
  balik di M5, veto batal — itu justru trigger `jebol`.
- **Penjebolan ganda bukan larangan**: engine tetap boleh entry, tapi menandai
  `short_target` (ambil cuan cepat, jangan hold).
- **TP/SL tidak dipasang di broker** (`ATTACH_SL=0`, `ATTACH_TP=0`) sesuai `.md:109`.
  TP dihitung hanya sebagai level alert; daftar TP boleh kosong.

Stop (alert) = level jarum/jebol + 0,35×ATR M1; TP1–3 = support/resistance berikutnya;
invalidasi = resis/sup H1 ± 2×ATR M15.
