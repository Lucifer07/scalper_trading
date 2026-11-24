# Integrasi order MT4 — EA bridge + AI advisor + risk guard

Dokumen ini menjelaskan jalur eksekusi order `xau-monitor`. Sifatnya
**opt-in**: tanpa konfigurasi eksplisit, repo tetap monitor read-only seperti
sebelumnya.

## 1. Temuan konektivitas (fakta, bukan asumsi)

| Item | Status saat dokumen ini ditulis |
|---|---|
| MT4 terpasang | ❌ tidak ada `terminal.exe` di mesin ini |
| MT5 terpasang | ✅ `/home/jayz/.mt5` (wine), build 5370, akun `MonexTest-Demo` |
| MT5 Python API | ✅ `MetaTrader5` 5.0.6231 di `C:\Python312` (wine) |
| Simbol XAUUSD di akun demo itu | ❌ hanya `BELAJAR`, `BTCUSD`, `KOIN1`, `TEST*`, `AUDCAD.m` |
| MCP MT4 | ❌ belum ada di mesin ini → dibuat di repo ini (`tools/mcp_mt4.py`) |

Konsekuensinya:

- **MT4 tidak punya API eksternal resmi.** Satu-satunya jalur adalah EA MQL4
  sebagai jembatan (yang dipakai repo ini) atau layanan cloud pihak ketiga.
- Semua kode Python di repo ini **sudah diuji end-to-end terhadap simulator EA**
  (`tools/fake_ea.py`, 110 test hijau). Yang **belum** diuji: EA di terminal
  MT4 sungguhan dengan broker yang menyediakan XAUUSD. Itu menunggu kredensial
  akun dari Anda.

## 2. Arsitektur

```
MT4 EA bridge (bar M1 native broker — SATU-SATUNYA sumber tick)
        │ ticks → M1 → TF snapshots
        ▼
   Engine Coach Tom ── Signal(ENTRY_SELL/BUY, zone, stop, tps, invalid)
        │
        ▼
   OrderIntent (OrderIntent, harga basis tick MT4)
        │
        ├─► Advisor (LLM, timeout 5s, fail-closed)
        │     approve | reject | adjust (maks ±0.5×ATR M1)
        │
        ├─► RiskGuard — OTORITAS TERAKHIR (deterministik)
        │     basis, spread, stoplevel, geometri, lot dari risk %, margin,
        │     satu posisi per magic, dedupe sinyal
        │
        ▼
   Mt4Link  ──TCP 127.0.0.1:8770──►  EA XauBridge (MQL4)
        │                                │
        │                                ▼
        │                          OrderSend(buy/sell, SL, TP)
        ▼
   Telegram (hasil + alasan AI)  ·  orders.jsonl (audit)
```

Prinsip yang mengikat:

1. **AI tidak pernah menjadi sumber angka final.** Ia boleh
   menyetujui/menolak/menggeser level dalam band dan **mengusulkan volume**
   (`lots`), tetapi guard deterministik yang mengunci: floor ke `lotstep`,
   clamp ke `lot_min..min(MAX_LOT, lot_max)`, cek margin, dan tolak bila usulan
   di bawah `lot_min`. Angka final selalu dari guard, bukan dari model.
2. **Fail-closed.** AI timeout, JSON rusak, atau EA putus ⇒ **tidak ada order**,
   dilaporkan ke Telegram dan audit log. Tidak ada retry diam-diam.
3. **EA sebagai TCP client** (dial out). Lebih sedikit kode berisiko di MQL4
   daripada `bind/listen/accept` dengan `fd_set` buatan tangan.

## 3. Protokol wire (pipe, satu baris per pesan)

| Arah | Format |
|---|---|
| Python → EA | `<id>|<cmd>|k=v;k=v` |
| EA → Python | `<id>|ok=1|k=v;...` atau `<id>|ok=0|err=<kode>;msg=<teks>` |

Nilai tidak boleh mengandung `|`, `;`, `=`, newline (disanitasi di boundary).
Alasan memilih pipe, bukan JSON: MQL4 tidak punya parser JSON; parser tangan
di EA adalah sumber bug yang tidak bisa diuji di sini.

Perintah: `ping`, `symbol_info`, `rates`, `positions`, `order_send`,
`order_close`, `order_modify`. Tidak ada streaming tick — monitor sudah punya feed sendiri;
EA hanya dipakai untuk eksekusi dan verifikasi posisi.

## 4. Pemasangan EA (MT4)

1. Copy `ea/XauBridge.mq4` **dan** `ea/XauBridgeCommands.mqh` ke
   `MQL4/Experts/` (keduanya harus satu folder).
2. MetaEditor → buka `XauBridge.mq4` → **F7** (Compile). Perbaiki error bila
   versi build MT4 Anda berbeda; kode memakai API MQL4 standar.
3. MT4 → **Tools → Options → Expert Advisors**:
   - centang **Allow automated trading**
   - centang **Allow DLL imports** (wajib: `ws2_32.dll`)
4. Buka chart mana pun (tidak harus XAUUSD) → drag EA → tab **Common**:
   centang *Allow live trading* → OK.
5. Input EA:
   | Input | Arti |
   |---|---|
   | `InpHost` / `InpPort` | harus sama dengan `MT4_BRIDGE_PORT` (default `8770`) |
   | `InpAllowTrading` | **lapis kedua arming**; `false` = read-only mutlak |
   | `InpMagic` | harus sama dengan `MT4_MAGIC` |
   | `InpMaxLot` / `InpMaxSpreadPoints` | batas keras di sisi terminal |
6. Pastikan simbol (mis. `XAUUSD` atau `XAUUSD.m`) ada di **Market Watch**.
7. Log terminal: `XauBridge init ...`, lalu `XauBridge terhubung ke server
   Python` begitu monitor jalan. EA mencoba reconnect tiap ±1 detik.

> **Penting:** EA hanya boleh di-arm (`InpAllowTrading=true`) setelah simulasi
> dan uji 1 order minimum di akun demo.

## 5. Arming berlapis (semua harus ON)

| Lapis | Lokasi | Default |
|---|---|---|
| 1. Python path | `ORDER_ENABLED=1` | `0` |
| 2. Python gate | `MT4_ALLOW_TRADING=1` | `0` |
| 3. EA gate | input `InpAllowTrading=true` | `false` |
| 4. Broker/terminal | *Allow live trading*, akun bukan read-only | — |

Config memvalidasi lapis 1 & 2: kalau keduanya tidak sama, `load_settings()`
**berhenti** (`SystemExit`) — tidak ada kombinasi setengah yang diizinkan.

## 6. Konfigurasi

Tambahan di `.env` (lihat `.env.example`):

| Variabel | Default | Catatan |
|---|---|---|
| `ORDER_ENABLED` | `0` | master switch jalur order |
| `MT4_ALLOW_TRADING` | `0` | harus sama dengan `ORDER_ENABLED` |
| `MT4_BRIDGE_PORT` | `8770` | port server Python yang di-dial EA |
| `MT4_SYMBOL` | `SYMBOL` | nama simbol di broker (bisa beda, mis. `XAUUSD.m`) |
| `MT4_MAGIC` | `20261116` | magic dasar; **sniper = base, handgun = base+1** (posisi terpisah) |
| `MODES` | `sniper` | daftar mode paralel (`sniper,handgun`); yang pertama trigger dieksekusi |
| `MAX_POSITIONS` | `2` | batas total posisi lintas mode |
| `MT4_DEVIATION` | `30` | slippage (poin) |
| `RISK_PERCENT` | `0.5` | risiko per order, % equity (0–5) |
| `MAX_LOT` | `0.10` | batas lot di sisi Python (plafon keras) |
| `MAX_SPREAD_POINTS` | `50` | tolak bila spread lebih lebar |
| `MAX_BASIS` | `3.0` | tolak bila harga feed vs broker menyimpang lebih dari ini |
| `MT4_BRIDGE_HOST` | `127.0.0.1` | non-loopback butuh `MT4_REMOTE_ALLOW=1` |
| `MT4_REMOTE_ALLOW` | `0` | izin bind ke LAN — **port order tanpa autentikasi** |
| `ATTACH_TP` | `1` | `1` = pasang TP terdekat; `0` = SL saja |
| `ORDER_DEDUPE_SECONDS` | `300` | jendela anti-order-ganda |
| `ADVISOR_URL` | kosong | backend HTTP (OpenAI-compatible). Kosong + CLI kosong = **tanpa AI** |
| `ADVISOR_MODEL` / `ADVISOR_API_KEY` | — | untuk backend HTTP |
| `ADVISOR_CLI_MODEL` | kosong | backend CLI, mis. `opencode-go/deepseek-v4.1-flash` |
| `ADVISOR_BIN` | `opencode` | binary CLI |
| `ADVISOR_AGENT` | kosong | `--agent` opencode (opsional) |
| `ADVISOR_TIMEOUT` | `30.0` | detik; timeout ⇒ tidak order |
| `ADVISOR_CLI_RETRIES` | `1` | percobaan ulang bila CLI timeout (0–3); total kegagalan tetap fail-closed |
| `ADVISOR_BAND_ATR` | `0.5` | band geser SL/TP oleh AI (×ATR M1) |
| `HISTORY_SOURCE` | `auto` | `mt4` = seed H1/H4/D1/W1 dari broker; `yahoo` = futures `GC=F` |
| `RULES_MD` | `~/ringkasan-scalping-xau-coach-tom.md` | sumber konteks AI |
| `RULES_MD_MAX_CHARS` | `11000` | `.md` utuh = 10.019 char; nilai <10.019 memotong bagian akhir |
| `MAX_DRIFT` | `1.0` | tolak bila harga broker bergerak > ini selama review AI |
| `ORDER_LOG` | `orders.jsonl` | audit JSONL |

## 6b. AI advisor via OpenCode CLI (tanpa API key)

Backend default yang dipakai di mesin ini: `ADVISOR_CLI_MODEL=opencode-go/deepseek-v4.1-flash`.
Tidak ada key yang disimpan — `opencode run` memakai auth OpenCode yang sudah ada.

```bash
# .env
ADVISOR_CLI_MODEL=opencode-go/deepseek-v4.1-flash
ADVISOR_TIMEOUT=30
RULES_MD_MAX_CHARS=1500
```

Isi prompt (terverifikasi): `.md` utuh 10.018 char + **blok kalibrasi 1.293 char**
(semua ambang engine: pos20, kelipatan ATR, wick %, lookback) + batasan keras +
skema JSON. Fakta yang dikirim per sinyal juga memuat angka mentah
(`metrics`: pos20 per TF, ATR M1/M5/M15, wick & body candle M5 terakhir,
`zone_dist_atr_m15`, `m5_thrust_threshold`) supaya model membandingkan dengan
ambang, bukan menebak.

Hasil pengukuran nyata (prompt penuh + direktori scratch):

| Skenario | Latency |
|---|---|
| CLI di `/tmp` (tanpa proyek), prompt ±2.5 KB | 14–26 s |
| CLI dijalankan dari root repo | **> 45 s** (agent menjelajah file) |
| Bersamaan dengan sesi OpenCode lain (kontensi akun) | bisa > 60 s |

Konsekuensi desain yang sudah diterapkan:

1. `CliAdvisor` menjalankan CLI di direktori scratch (`/tmp/xau-monitor-advisor`),
   bukan di repo — tanpa itu, agent mencoba membaca proyek dan latensi meledak.
2. Eksekusi order berjalan di **worker thread** (`ThreadPoolExecutor(1)`), jadi
   loop tick tidak berhenti menunggu LLM. Hasil dikirim ke Telegram dari loop.
3. `MAX_DRIFT` menjaga agar harga tidak bergerak jauh selama review; kalau
   lewat batas ⇒ `price_moved`, order dibatalkan.
4. Timeout = fail-closed. Lebih baik kehilangan satu setup daripada entry buta.
   Timeout percobaan pertama umumnya transient (cold start CLI / kontensi akun
   opencode saat sesi lain aktif), jadi CLI dicoba ulang `ADVISOR_CLI_RETRIES`
   kali (default 1) dengan timeout yang sama sebelum menyatakan unavailable.

Jika butuh lebih cepat: pakai varian `#low`
(`opencode-go/deepseek-v4.1-flash#low`) atau matikan AI
(`ADVISOR_CLI_MODEL` kosong) dan andalkan level engine.

## 6c. Memasukkan akun demo MT4

**Jangan kirim password di chat.** Kredensial diisi di terminal MT4, bukan di
repo/`.env`. Yang saya butuhkan hanya:

1. Nama broker + server demo (mis. `BrokerName-Demo`).
2. Nomor login demo.
3. Nama simbol XAUUSD persis seperti di **Market Watch** (`XAUUSD`, `XAUUSD.m`, …).
4. MT4 akan berjalan di mana:
   - **mesin ini (Linux + wine)** → saya siapkan prefix `~/.mt4`, instal
     installer MT4 dari broker Anda, lalu EA dipasang di `MQL4/Experts/`;
   - **PC Windows lain** → EA dial ke IP LAN mesin ini:
     `MT4_BRIDGE_HOST=0.0.0.0` + `MT4_REMOTE_ALLOW=1` (pahami risikonya: port
     order tanpa autentikasi; batasi firewall ke IP PC itu, atau pakai SSH
     tunnel/VPN).

Langkah di mesin ini (kalau MT4 di sini):

```bash
WINEARCH=win32 WINEPREFIX=~/.mt4 winecfg          # prefix 32-bit (MT4 32-bit)
WINEARCH=win32 WINEPREFIX=~/.mt4 wine ~/Downloads/mt4setup.exe
# copy EA
cp ea/XauBridge.mq4 ea/XauBridgeCommands.mqh \
   ~/.mt4/drive_c/Program\ Files/*/MQL4/Experts/
```

Login: buka terminal → **File → Login to Trade Account** (isi manual, centang
*Save account information*). Jangan pakai argumen `/login:` — password terlihat
di `ps`/history.

Urutan verifikasi yang saya sarankan:

1. `ORDER_ENABLED=0` (default) → jalankan monitor → EA menempel di chart →
   pastikan log EA menulis `XauBridge terhubung`.
2. Jalankan MCP: `mt4_ping` harus menampilkan akun & `trading_allowed=0`.
3. `mt4_symbol` untuk XAUUSD → catat `stoplevel`, `lotstep`, `marginreq`.
4. Baru naikkan: `MT4_ALLOW_TRADING=1` + `ORDER_ENABLED=1` + EA
   `InpAllowTrading=true`, dengan `MAX_LOT` minimum dan `RISK_PERCENT=0.1`.
5. Kirim satu order manual lewat MCP (`mt4_order`), cek `orders.jsonl` dan
   riwayat terminal. Setelah itu baru percayakan ke monitor.

## 7. Menjalankan

```bash
# 1) tanpa MT4: simulator EA (menguji jalur penuh)
make sim                 # terminal lain

# 2) monitor (dry-run Telegram boleh)
ORDER_ENABLED=0 make run

# 3) dengan order (setelah EA terpasang & di-arm)
ORDER_ENABLED=1 MT4_ALLOW_TRADING=1 make run
```

Urutan startup yang benar: **EA terpasang di chart → jalankan monitor** (monitor
yang membuka port). Kalau EA start lebih dulu, ia akan reconnect otomatis.

## 8. MCP MT4 (alat operator/agen)

`tools/mcp_mt4.py` membungkus bridge yang sama sebagai MCP server stdio:

```jsonc
// ~/.config/opencode/opencode.json → mcp
"mt4-bridge": {
  "type": "local",
  "command": ["/home/jayz/xau-monitor/.venv/bin/python", "-m", "tools.mcp_mt4",
              "--port", "8770", "--symbol", "XAUUSD"]
}
```

Tool read-only selalu ada: `mt4_ping`, `mt4_quote`, `mt4_symbol`,
`mt4_positions`. Tool tulis (`mt4_order`, `mt4_close`, `mt4_modify`) hanya
muncul dengan `--allow-trading`, dan tetap ditolak EA bila `InpAllowTrading`
false. **Tool tulis tidak memakai sizing risiko** — lot wajib eksplisit dan
divalidasi terhadap batas broker; sizing otomatis hanya ada di jalur monitor.

Port hanya bisa dipegang satu proses: jalankan MCP saat monitor berhenti, atau
ubah `--port` (EA harus ikut diubah).

## 9. Kepatuhan terhadap `.md` (ATTACH_SL=0, ATTACH_TP=0)

Dokumen Coach Tom (`ringkasan-scalping-xau-coach-tom.md:109,122-123`) menyatakan
**tidak memasang TP/SL otomatis** dan exit manual di TF M1. Implementasi default
sekarang mengikuti itu:

- `ATTACH_SL=0`, `ATTACH_TP=0` → order dikirim dengan `sl=0`, `tp=0`.
- Karena tidak ada jarak SL, **`RISK_PERCENT` tidak dipakai** sebagai sizing.
  Volume ditentukan **usulan advisor** (`lots` di jawaban JSON), lalu dikunci
  guard: floor ke `lotstep`, clamp ke `lot_min..min(MAX_LOT, lot_max)`, tolak
  bila di bawah `lot_min`, dan cek margin. `MAX_LOT` sekarang `0.05` (plafon
  keras; advisor hanya bisa memilih 0.01–0.05 sampai nilai itu dinaikkan).
  Bila advisor tidak mengirim `lots` (mis. AI nonaktif), guard jatuh ke
  `min(MAX_LOT, lot_max)`. Guard tetap memeriksa spread, basis, drift, margin,
  dan satu-posisi-per-magic.
- AI advisor: dengan proteksi broker mati, hanya `approve`/`reject` yang berlaku;
  `adjust` level ditolak (`tanpa SL/TP di broker: adjust level tidak berlaku`).

### 9b. Mekanisme entry & exit aktif (2026-11-19)

- **Entry = pending LIMIT di level stop engine** (`otype=limit`, `price=SL engine`),
  `sl=0`, `tp=0`. Untuk BUY limit dipasang di bawah harga; SELL di atas.
  EA menolak bila limit di sisi pasar yang salah atau lebih dekat dari stoplevel.
- **Offset mendekati pasar**: harga limit digeser sebesar
  `min(LIMIT_OFFSET_PCT% × (high−low) candle M5 terakhir, LIMIT_OFFSET_MAX_PIPS pip)`
  — **BUY `+offset`**, **SELL `−offset`** — supaya limit tidak terlalu jauh dan
  lebih mudah terisi. Nilai di-clamp agar tetap di sisi pasar yang valid dan di
  luar stoplevel broker.
- **Pending berlaku `PENDING_TTL_SECONDS` (default 300 = 5 menit)**; bila belum
  tersentuh, dibatalkan otomatis (`order_delete`).
- **Setelah terisi** (ticket pending menjadi posisi): **trailing TP**. Profit
  dipantau dalam satuan harga; bila puncak profit tercapai lalu retrace
  ≥ `TRAIL_RETRACE_PCT` (default 20%) dari puncak, posisi ditutup (`order_close`).
  Puncak di bawah 2× spread diabaikan agar tidak menutup karena noise.
- **Tidak ada reversal 45%** — mekanisme itu dibatalkan pemilik. Tidak ada SL
  broker; risiko posisi yang bergerak berlawanan sepenuhnya manual.
- **Layering (opsional, toggle dari panel)**: saat ON, AI menentukan jumlah layer
  (`layers`, minimal `LAYERING_MIN=2`) berdasarkan equity, `free_margin`
  (sudah memperhitungkan floating), dan `margin_per_layer`. **Tidak ada batas
  jumlah layer**; yang membatasi adalah **total eksposur**: margin batch ≤
  `LAYERING_MAX_MARGIN_PCT` (default 2%) dari equity DAN ≤
  `free_margin × LAYERING_MARGIN_SAFETY`. Guard memotong jumlah bila melebihi.
  Semua layer dipasang sekaligus; jarak = `max(offset, 1 pip)`; layer ke-k lebih
  dalam (`BUY: limit1 − (k−1)×spacing`, `SELL: limit1 + (k−1)×spacing`).
  Exit tetap trailing 20% per layer. Endpoint:
  `POST /api/layering {"enabled": true|false}` (control) dan `/api/layering`
  (panel, proxy).
- Semua aksi (pending, expired, filled, trail_close, layering_reject) tercatat
  di `ORDER_LOG`.

Konsekuensi yang harus disadari: **tidak ada proteksi otomatis** bila monitor,
VPS, atau koneksi mati saat posisi terbuka, dan posisi yang tidak pernah untung
tidak akan ditutup otomatis. Kalau ingin pengaman darurat, set `ATTACH_SL=1`
(SL dari level engine) tanpa TP.

## 10. Batas & risiko yang harus diterima

1. **Belum diuji di MT4 nyata.** EA dikompilasi di mesin Anda nanti; kirimkan
   error kompilasi bila ada, saya perbaiki.
2. **Feed ≠ broker.** Harga FIX kantor bisa berbeda beberapa dolar dari broker.
   Guard menggeser level dengan `basis` dan menolak bila `|basis| > MAX_BASIS`.
   Selalu cek `basis` di audit log order pertama.
3. **Latency AI.** `opencode run` (DeepSeek V4.1 Flash) terukur 14–26 detik,
   dan bisa >60 detik saat akun OpenCode dipakai bersamaan. Loop tick tidak
   diblokir (worker thread), tetapi entry tertunda; `MAX_DRIFT` membatalkan bila
   harga sudah bergerak > batas. Untuk scalping lebih ketat: matikan AI atau
   pakai varian `#low`.
4. **Satu koneksi EA.** Monitor dan MCP tidak bisa memakai port yang sama.
5. **Nama simbol broker.** `XAUUSD` di satu broker bisa `XAUUSD.m`, `GOLD`,
   atau kontrak Dec. Set `MT4_SYMBOL` persis seperti di Market Watch.
6. **`MODE_MARGINREQUIRED` = 0** pada sebagian simbol ⇒ guard menolak
   (`margin_unknown`). Itu disengaja: tanpa data margin, sizing tidak bisa
   dipertanggungjawabkan.

## 11. Audit & troubleshooting

`orders.jsonl` (satu baris JSON per kejadian):

| `event` | Arti | Tindakan |
|---|---|---|
| `sent` | order diterima EA | cek `plan.basis`, `plan.lots`, `ticket` |
| `guard_reject` | guard menolak | lihat `code` (`spread_too_wide`, `basis_too_wide`, `lot_below_min`, …) |
| `advisor_reject` | AI menolak | baca `rationale` |
| `advisor_unavailable` | AI timeout/rusak | cek endpoint/model; bot sengaja tidak order |
| `ea_reject` | broker/EA menolak | `code` = kode `err` dari EA/broker |
| `ea_unavailable` | bridge putus | cek EA menempel di chart, port, `InpAllowTrading` |

Diagnosa cepat:

```bash
tail -f orders.jsonl                     # audit keputusan & order
journalctl --user -u xau-monitor -f      # log monitor (EA connect/disconnect)
```

Cek konektivitas EA lewat MCP (`mt4_ping`) — jangan kirim frame mentah ke port
bridge, karena port itu milik link monitor dan frame asing akan dianggap rusak.
