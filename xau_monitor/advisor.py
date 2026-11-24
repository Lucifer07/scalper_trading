"""AI advisor: reviewer + veto, bukan sumber kebenaran harga.

Batasan yang mengikat model:
- hanya boleh `approve`, `reject`, atau `adjust` dalam band ±`band_atr`×ATR M1
  dari angka engine (default 0.5) — di luar band = ditolak, bukan di-clamp;
- tidak boleh membalik arah atau mengubah simbol;
- **volume diusulkan model** (`lots`) tetapi tetap DIKUNCI guard deterministik:
  di-floor ke `lotstep`, di-clamp `lot_min..min(MAX_LOT, lot_max)`, dan wajib
  lolos cek margin. Angka final selalu dari guard, bukan dari model;
- timeout / jawaban tidak sesuai skema = fail-closed (tidak ada order).

Konteks aturan diambil dari `.md` Coach Tom: dokumen itu menetapkan disiplin
cut-loss di TF M1 dan "TP cepat, tidak serakah" — bukan formula SL/TP. Karena
itu angka awal selalu dari engine, model hanya mengoreksi.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal, Protocol

log = logging.getLogger("advisor")

ACTIONS = ("approve", "reject", "adjust")
MAX_RATIONALE = 400


# Kalibrasi numerik engine. Berasal dari implementasi (bukan dari .md — .md hanya
# kualitatif). AI WAJIB memakainya untuk menilai apakah bentuk pola benar-benar
# memenuhi ambang, bukan menebak.
CALIBRATION = """Kalibrasi engine (angka ini implementasi, bukan kutipan .md):
- tren TF: DOWN jika pos20<=35% dan net8 < -0.5xATR; UP jika pos20>=65% dan net8 > +0.5xATR; selain itu FLAT.
- sniper: D1+H4+H1 wajib seirama (tidak selaras = tidak trading). handgun: M15 menentukan arah.
- sniper: D1+H4+H1 hanya menentukan ARAH. Gate entry = ekstrem M15 BERLAWANAN arah:
  arah UP -> wajib M15 pos20 <=20% (turun dalam, pullback ke support) -> BUY;
  arah DOWN -> wajib M15 pos20 >=80% (naik tinggi, pullback ke resistance) -> SELL.
- ekstrem handgun: pos20 M15 >=80% (naik tinggi) -> SELL; <=20% (turun dalam) -> BUY.
  Handgun tidak butuh D1/H4+H1 seirama.
- zona: cluster pivot M15 (+M30 top1, +H1 top1), toleransi gabung 0.25xATR M15; harga harus <=1.0xATR M15 dari level.
- jebol ganda: 6 candle M15 menyapu zona +-0.5xATR -> hanya memperkecil target, bukan larangan.
- kocokan: 6 candle M5, toleransi sentuh 0.2xATR M5, minimal 2 sentuhan dan 2 flip arah.
- dorongan terakhir (thrust): candle M5 >=1.2xATR M5 berlawanan bias, di 6 candle terakhir (bukan candle terakhir).
- jarum tunggal: wick >=55% dan close searah bias.
- jarum berurutan: >=2 dari 3 candle M5 wick >=45%, close terakhir searah.
- jebol-jarum: wick >=50%, ditembus lalu close balik; lookback 6 candle M5.
- menit terakhir M15: 120 detik terakhir bucket 900s, body >=50% range melawan bias -> tunda.
- stop alert: level jarum/jebol +-0.35xATR M1, minimum 50 point simbol
  (XAUUSD digits=2 -> 0,50; GBPUSD digits=5 -> 0,00050).
- TP: support/resistance berikutnya (maks 3 level); boleh kosong.
- lot: KAMU yang mengusulkan `lots` (lihat instruksi VOLUME). Batas keras ada di
  DATA (`lot_min`, `lot_max_allowed`, `lot_step`). Guard memotong ke batas itu;
  usulan di bawah lot_min = order ditolak.
- conviction (semua TF seirama) HANYA syarat untuk hold posisi, BUKAN syarat entry.
  Mode handgun memang entry tanpa seirama TF besar: jangan reject hanya karena D1/H4/H1
  tidak searah. Reject handgun hanya bila pola/ekstrem/zona/trigger tidak valid.
- Chasing diukur dari pos20 TF ENTRY (M15/M5) saat sinyal: ~100% untuk BUY atau ~0%
  untuk SELL = alasan reject yang SAH. Ini konsisten dengan gate ekstrem: BUY hanya
  lolos saat M15 turun dalam, SELL saat M15 naik tinggi. Jangan pakai pos20 D1.
- atr_m1 = 0 berarti candle M1 belum siap: jangan mengarang; nilai SL/TP diabaikan
  karena broker tidak memasangnya, jadi fokus pada pola dan zona.
"""


class AdvisorUnavailable(RuntimeError):
    """Model tidak bisa dihubungi / jawaban bukan JSON valid (fail-closed)."""


class AdvisorRejected(RuntimeError):
    """Model menolak, atau usulannya melanggar band/skema."""


@dataclass(frozen=True)
class AdvisorContext:
    symbol: str
    side: str
    mode: str
    price: float
    zone: float
    stop: float
    tps: tuple[float, ...]
    invalid: float
    atr_m1: float
    reasons: tuple[str, ...]
    broker_protection: bool = True
    metrics: tuple[tuple[str, float], ...] = ()
    digits: int = 2
    balance: float = 0.0
    free_margin: float = 0.0
    margin_per_lot: float = 0.0
    lot: float = 0.0
    layering: bool = False
    layering_min: int = 2
    margin_safety: float = 0.9
    max_margin_pct: float = 2.0
    equity: float = 0.0
    lot_min: float = 0.01
    lot_step: float = 0.01

    def facts(self) -> dict[str, object]:
        d = max(0, int(self.digits))
        per_layer = self.margin_per_lot * self.lot
        max_by_margin = (int(self.free_margin * self.margin_safety // per_layer)
                         if per_layer > 0 else 0)
        max_by_exposure = (int(self.equity * self.max_margin_pct / 100.0 // per_layer)
                           if per_layer > 0 else 0)
        effective = min(max_by_margin, max_by_exposure)
        return {
            "symbol": self.symbol, "side": self.side, "mode": self.mode,
            "price_feed": round(self.price, d), "zone": round(self.zone, d),
            "engine_stop": round(self.stop, d),
            "engine_tps": [round(t, d) for t in self.tps],
            "invalid_level": round(self.invalid, d),
            "atr_m1": round(self.atr_m1, d + 1),
            "engine_reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "broker_protection": self.broker_protection,
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "free_margin": round(self.free_margin, 2),
            "margin_per_lot": round(self.margin_per_lot, 2),
            "margin_per_layer": round(per_layer, 2),  # = margin_per_lot x lot_max_allowed
            "lot_min": self.lot_min,
            "lot_step": self.lot_step,
            "lot_max_allowed": round(self.lot, 2),
            "layering": self.layering,
            "layering_min": self.layering_min,
            "max_margin_pct": self.max_margin_pct,
            "max_layers_by_margin": max_by_margin,
            "max_layers_by_exposure": max_by_exposure,
            "max_layers_effective": effective,
        }


@dataclass(frozen=True)
class AdvisorDecision:
    action: Literal["approve", "reject", "adjust"]
    sl: float | None = None
    tps: tuple[float, ...] = ()
    confidence: float = 0.0
    rationale: str = ""
    layers: int = 0            # jumlah layer saat layering aktif (0 = tidak ditentukan)
    lots: float = 0.0          # usulan volume; 0 = tidak ditentukan -> guard pakai MAX_LOT


@dataclass(frozen=True)
class Levels:
    stop: float
    tps: tuple[float, ...]
    lots: float | None = None  # usulan volume dari advisor (belum divalidasi guard)


class Advisor(Protocol):
    @property
    def name(self) -> str: ...

    def review(self, ctx: AdvisorContext) -> AdvisorDecision: ...


class NullAdvisor:
    """AI nonaktif: angka engine diteruskan apa adanya (jalur deterministik)."""

    @property
    def name(self) -> str:
        return "null"

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        return AdvisorDecision("approve", rationale="AI nonaktif — level engine dipakai")


def digest_rules(path: str, limit: int) -> str:
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise AdvisorUnavailable(f"RULES_MD tidak terbaca ({path}): {exc}") from exc
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise AdvisorUnavailable(f"RULES_MD kosong: {path}")
    if len(text) > limit:
        text = text[:limit] + "\n[...dipotong; lihat dokumen asli]"
    return text


def build_system_prompt(rules: str, band_atr: float,
                        protection: bool = True, symbol: str = "XAUUSD",
                        layering_min: int | None = None) -> str:
    guard = ("" if protection else
             "- Broker TIDAK memasang SL/TP (patuh dokumen: exit manual di TF M1). "
             "Maka hanya approve/reject yang berlaku; angka sl/tps diabaikan.\n")
    layer = ""
    if layering_min is not None:
        layer = (f"- LAYERING AKTIF: tentukan \"layers\" (integer, minimal {layering_min}; "
                 "lots = per layer) "
                 "berdasarkan equity, free_margin, dan margin_per_lot x lots di DATA "
                 "(margin_per_layer di DATA dihitung pada lot_max_allowed, bukan pada "
                 "lot yang kamu pilih). "
                 "Jumlah efektif dibatasi max_layers_effective (total eksposur "
                 "maksimum 2% equity + margin bebas); guard memotong bila berlebihan. "
                 "Jika layering tidak aktif, layers boleh 0/1.\n")
    return (
        f"Kamu reviewer eksekusi trading {symbol}, bukan penentu arah. Aturan sumber:\n"
        f"{rules}\n\n"
        "Tugasmu: periksa apakah setup engine layak dieksekusi SEKARANG.\n"
        "Balas HANYA JSON: {\"action\":\"approve|reject|adjust\","
        "\"sl\":number|null,\"tps\":[number]|[],\"lots\":number,"
        "\"confidence\":0..1,\"rationale\":\"<=300 karakter\"}.\n"
        + CALIBRATION + "\nBatasan keras:\n"
        f"- Boleh geser SL/TP maksimal {band_atr:g}x ATR M1 dari angka engine.\n"
        "- VOLUME: setiap approve/adjust WAJIB menyertakan \"lots\" (number). Batas "
        "keras ada di DATA: lot_min .. lot_max_allowed, kelipatan lot_step. Guard "
        "memotong ke batas itu dan MENOLAK usulan di bawah lot_min. Dasarnya: "
        "kualitas pola, confidence, dan volatilitas (atr_m5/atr_m15 di metrics) — "
        "pola kuat + volatilitas normal mendekati lot_max_allowed; ragu atau "
        "volatilitas tinggi pakai lot_min. Wajib muat margin: lots x margin_per_lot "
        "harus < free_margin (sisakan ruang; jangan pakai seluruh free margin). "
        "Kalau layering aktif, lots = per layer.\n"
        "- Tidak boleh membalik arah atau mengganti simbol. Volume kamu usulkan, "
        "guard yang mengunci.\n"
        "- Jangan mengarang level baru; kalau ragu, pakai action=reject.\n"
        "- Dokumen sumber melarang TP/SL otomatis dan menekankan close manual di TF M1. "
        "Kalau broker dipasangi SL/TP, itu hanya pengaman darurat, bukan target. "
        "Jebol ganda bukan alasan reject, tapi alasan target pendek.\n"
        "- rationale: alasan singkat berbasis gate yang diberikan, bukan prediksi harga.\n"
        f"- Aturan sumber memakai contoh angka harga/uang untuk satu simbol. Untuk "
        f"{symbol}, abaikan semua angka contoh itu; pakai hanya prinsip price-action "
        f"dan angka kalibrasi / metrics di payload.\n"
        + layer
        + guard
    )


class HttpAdvisor:
    """OpenAI-compatible chat completions (Ollama/vLLM/OpenRouter/OpenAI)."""

    def __init__(self, url: str, model: str, api_key: str, timeout: float,
                 system_prompt: str) -> None:
        self._url = url if url.endswith("/chat/completions") else url.rstrip("/") + "/chat/completions"
        self._model = model
        self._key = api_key
        self._timeout = timeout
        self._system = system_prompt

    @property
    def name(self) -> str:
        return f"http:{self._model}"

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": json.dumps(ctx.facts(), ensure_ascii=False)},
            ],
        }
        content = self._post({**payload, "response_format": {"type": "json_object"}})
        if content is None:
            content = self._post(payload)  # provider tanpa json mode
        if content is None:
            raise AdvisorUnavailable("provider menolak permintaan (json mode & plain)")
        return parse_decision(content)

    def _post(self, payload: dict) -> str | None:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(self._url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            log.warning("advisor HTTP %s: %s", exc.code, exc.read()[:200])
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdvisorUnavailable(f"advisor tidak terjangkau: {exc}") from exc
        try:
            data = json.loads(raw)
            return str(data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdvisorUnavailable(f"respons provider tidak dikenali: {exc}") from exc


class CliAdvisor:
    """Pakai `opencode run` — auth opencode-go dipakai apa adanya, tanpa API key.

    NDJSON dari `--format json` diparse: ambil part bertipe ``text``, lalu cari
    objek JSON pertama yang punya field ``action``. Semua kegagalan (binary
    hilang, exit != 0, timeout, tanpa JSON) = `AdvisorUnavailable` (fail-closed).
    """

    def __init__(self, model: str, timeout: float, system_prompt: str,
                 binary: str = "opencode", agent: str = "",
                 cwd: str | None = None, retries: int = 1) -> None:
        self._model = model
        self._timeout = timeout
        self._system = system_prompt
        self._bin = binary
        self._agent = agent
        self._retries = max(0, int(retries))
        # Direktori scratch: `opencode run` di dalam repo membuat agent menjelajah
        # file (latency 45s+). Tanpa proyek, jawabannya 15-20s.
        self._cwd = cwd or os.path.join(tempfile.gettempdir(), "xau-monitor-advisor")
        os.makedirs(self._cwd, exist_ok=True)

    @property
    def name(self) -> str:
        return f"cli:{self._model}"

    def review(self, ctx: AdvisorContext) -> AdvisorDecision:
        prompt = (f"{self._system}\n\nDATA (JSON):\n"
                  f"{json.dumps(ctx.facts(), ensure_ascii=False)}")
        cmd = [self._bin, "run", "--format", "json", "-m", self._model]
        if self._agent:
            cmd += ["--agent", self._agent]
        cmd.append(prompt)
        # Timeout percobaan pertama umumnya transient (cold start / kontensi akun
        # opencode), bukan keputusan "jangan trading": jadi dicoba sekali lagi.
        # Kedua percobaan gagal -> fail-closed seperti sebelumnya.
        attempts = self._retries + 1
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout, check=False, cwd=self._cwd)
                break
            except FileNotFoundError as exc:
                raise AdvisorUnavailable(f"binary {self._bin!r} tidak ditemukan") from exc
            except subprocess.TimeoutExpired as exc:
                if attempt >= attempts:
                    raise AdvisorUnavailable(
                        f"advisor CLI timeout {self._timeout:.0f}s "
                        f"({attempts}x percobaan)") from exc
                log.warning("advisor CLI timeout %ss (percobaan %d/%d) — ulangi",
                            self._timeout, attempt, attempts)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:200]
            raise AdvisorUnavailable(f"advisor CLI exit {proc.returncode}: {stderr}")
        text = _events_text(proc.stdout)
        obj = _find_json_object(text)
        if obj is None:
            raise AdvisorUnavailable(f"tidak ada JSON action di jawaban: {text[:200]!r}")
        return parse_decision(json.dumps(obj))


def _events_text(stdout: str) -> str:
    """Gabungkan part `text` dari NDJSON opencode; fallback ke stdout mentah."""
    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        if event.get("type") == "text" and part.get("type") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks) if chunks else stdout


def _find_json_object(text: str) -> dict | None:
    """Objek JSON pertama yang punya field `action` (tahan teks pembungkus)."""
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    return None


def parse_decision(content: str) -> AdvisorDecision:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AdvisorUnavailable(f"jawaban bukan JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AdvisorUnavailable("jawaban bukan objek JSON")
    action = data.get("action")
    if action not in ACTIONS:
        raise AdvisorUnavailable(f"action tidak valid: {action!r}")
    rationale = str(data.get("rationale", ""))[:MAX_RATIONALE]
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    try:
        layers = int(data.get("layers", 0) or 0)
    except (TypeError, ValueError):
        layers = 0
    layers = max(0, layers)
    # Volume: angka mentah dari model. Tidak valid / negatif -> 0 (guard pakai
    # MAX_LOT sebagai fallback). Nilai di luar batas broker di-clamp guard.
    try:
        lots = float(data.get("lots", 0) or 0)
    except (TypeError, ValueError):
        lots = 0.0
    if not math.isfinite(lots) or lots < 0:
        lots = 0.0
    if action != "adjust":
        return AdvisorDecision(action, None, (), confidence, rationale, layers, lots)

    sl_raw, tps_raw = data.get("sl"), data.get("tps")
    try:
        sl = float(sl_raw) if sl_raw is not None else None
        tps = tuple(float(x) for x in (tps_raw or ()))
    except (TypeError, ValueError) as exc:
        raise AdvisorUnavailable(f"adjust dengan angka tidak valid: {exc}") from exc
    if sl is None and not tps:
        raise AdvisorUnavailable("action=adjust tapi tidak ada sl/tps")
    return AdvisorDecision("adjust", sl, tps, confidence, rationale, layers, lots)


def apply_decision(ctx: AdvisorContext, decision: AdvisorDecision,
                   band_atr: float) -> Levels:
    if decision.action == "reject":
        raise AdvisorRejected(decision.rationale or "advisor menolak")
    lots = decision.lots if decision.lots > 0 else None
    if decision.action == "approve":
        return Levels(ctx.stop, ctx.tps, lots)
    if not ctx.broker_protection:
        raise AdvisorRejected("tanpa SL/TP di broker: adjust level tidak berlaku")
    band = band_atr * ctx.atr_m1
    if band <= 0:
        raise AdvisorRejected("ATR M1 tidak tersedia — usulan adjust tidak bisa divalidasi")
    stop = ctx.stop
    if decision.sl is not None:
        if abs(decision.sl - ctx.stop) > band:
            raise AdvisorRejected(
                f"SL usulan {decision.sl:.{ctx.digits}f} di luar band "
                f"{band:.{ctx.digits}f} dari {ctx.stop:.{ctx.digits}f}")
        stop = decision.sl
    tps = list(ctx.tps)
    if decision.tps:
        if len(decision.tps) != len(ctx.tps):
            raise AdvisorRejected(
                f"jumlah TP berubah ({len(decision.tps)} vs {len(ctx.tps)})")
        for idx, proposed in enumerate(decision.tps):
            if abs(proposed - ctx.tps[idx]) > band:
                raise AdvisorRejected(
                    f"TP{idx + 1} usulan {proposed:.{ctx.digits}f} di luar band "
                    f"{band:.{ctx.digits}f}")
            tps[idx] = proposed
    return Levels(stop, tuple(tps), lots)


def build_advisor(url: str, model: str, api_key: str, timeout: float,
                  rules_path: str, rules_limit: int, band_atr: float,
                  cli_model: str = "", advisor_bin: str = "opencode",
                  advisor_agent: str = "", protection: bool = True,
                  symbol: str = "XAUUSD",
                  layering_min: int | None = None,
                  cli_retries: int = 1) -> Advisor:
    """Pilih backend: HTTP (API key) atau CLI (auth opencode) — tidak boleh dua."""
    if url and cli_model:
        raise AdvisorUnavailable("ADVISOR_URL dan ADVISOR_CLI_MODEL diisi bersamaan — pilih satu")
    if cli_model:
        rules = digest_rules(rules_path, rules_limit)
        if not shutil.which(advisor_bin):
            raise AdvisorUnavailable(f"binary {advisor_bin!r} tidak ditemukan di PATH")
        return CliAdvisor(cli_model, timeout,
                          build_system_prompt(rules, band_atr, protection, symbol,
                                              layering_min),
                          advisor_bin, advisor_agent, retries=cli_retries)
    if not url:
        return NullAdvisor()
    if not model:
        raise AdvisorUnavailable("ADVISOR_URL diisi tapi ADVISOR_MODEL kosong")
    rules = digest_rules(rules_path, rules_limit)
    return HttpAdvisor(url, model, api_key, timeout,
                       build_system_prompt(rules, band_atr, protection, symbol,
                                           layering_min))
