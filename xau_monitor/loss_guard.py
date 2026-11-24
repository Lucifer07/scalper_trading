"""Hard loss stop: rugi akun >= limit -> tutup semua posisi + matikan app.

Kenapa di monitor, bukan di EA: satu-satunya angka akun (equity/balance) datang
dari jawaban `ping` EA yang hanya bisa dibaca proses pemilik bridge. EA tidak
punya state lintas simbol, jadi keputusan "rugi akun" harus di Python.

Kontrak yang disengaja:
- Baseline saldo dibekukan sekali di file state bersama antar instance, lalu
  trigger bila `equity - baseline <= -limit`. Realized + floating ikut dihitung.
- Trigger butuh `confirm` sampel berurutan: bacaan sesaat (EA baru sambung,
  tick kosong) tidak boleh memicu aksi yang tidak bisa dibatalkan.
- Latch ditulis atomik SEBELUM flatten: instance lain tidak mengulang close,
  dan monitor yang di-restart melihat latch -> tidak meng-arm jalur order.
- Flatten = batalkan pending limit DULU (kalau tidak, limit bisa terisi setelah
  app mati), lalu tutup posisi. Semua kegagalan dicatat, bukan ditelan.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .mt4_link import Mt4Error, Mt4Link
from .orders import AccountState, GuardRejection

log = logging.getLogger("loss_guard")

LATCH_TITLE = "TRADING LOCKED"


@dataclass(frozen=True)
class GuardConfig:
    """Konfigurasi hard stop. `limit_usd <= 0` = guard mati."""

    limit_usd: float
    poll_s: float = 5.0
    confirm: int = 2
    state_path: str = "state/loss_guard.json"
    latch_path: str = "state/trading_locked.json"
    lock_path: str = "state/loss_guard.lock"
    symbols_path: str = "symbols.json"
    stop_units: tuple[str, ...] = ()
    systemctl: str = "systemctl"


@dataclass(frozen=True)
class FlattenResult:
    closed: tuple[tuple[str, int, float], ...] = ()
    cancelled: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()


def _atomic_write(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.error("state guard %s rusak/tidak terbaca (%s) — dibuat ulang", path, exc)
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def _locked(path: str):
    """Lock antar-proses: lima instance berbagi satu akun broker."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LossGuard:
    """Satu instance memantau akun; yang lebih dulu breach melakukan aksi."""

    def __init__(self, cfg: GuardConfig, link: Mt4Link, notifier=None, *,
                 symbol: str, magic: int, clock=time.time,
                 stopper=None) -> None:
        self.cfg = cfg
        self._link = link
        self._notifier = notifier
        self._symbol = symbol.upper()
        self._magic = int(magic)
        self._clock = clock
        self._stopper = stopper or self._stop_units
        self._streak = 0
        self._next_poll = 0.0
        self._next_log = 0.0
        self._cleanup = False
        self.last_equity: float | None = None
        self.last_baseline: float | None = None
        self.locked = self._latch_payload() is not None
        if self.locked:
            log.error("%s: %s ada — jalur order TIDAK boleh di-arm",
                      LATCH_TITLE, cfg.latch_path)

    # ---- API -------------------------------------------------------
    def check(self) -> bool:
        """True = app harus berhenti; False = aman lanjut.

        - Latch ada dan flatten sudah tuntas -> True (berhenti).
        - Latch ada tapi posisi belum tertutup (mis. market closed) -> False
          supaya proses hidup dan mencoba flatten lagi tiap poll; runner wajib
          membaca `locked` untuk memblokir order baru.
        """
        if self.locked:
            return self._retry_cleanup()
        now = self._clock()
        if now < self._next_poll:
            return False
        self._next_poll = now + self.cfg.poll_s
        try:
            account = AccountState.from_wire(self._link.ping())
        except (Mt4Error, GuardRejection, OSError, ValueError, KeyError) as exc:
            log.debug("loss guard: snapshot akun gagal (%s) — dilewati", exc)
            return False
        baseline = self._baseline(account.balance)
        self.last_equity, self.last_baseline = account.equity, baseline
        loss = baseline - account.equity
        self._log_snapshot(account, baseline, loss)
        if loss < self.cfg.limit_usd - 1e-9:
            self._streak = 0
            return False
        self._streak += 1
        if self._streak < max(1, self.cfg.confirm):
            log.warning("loss guard: rugi %.2f (sampel %d/%d) — menunggu konfirmasi",
                        loss, self._streak, self.cfg.confirm)
            return False
        return self._trip(account, baseline, loss)

    # ---- baseline --------------------------------------------------
    def _baseline(self, balance: float) -> float:
        with _locked(self.cfg.lock_path):
            state = _read_json(self.cfg.state_path) or {}
            try:
                return float(state["baseline_balance"])
            except (KeyError, TypeError, ValueError):
                pass
            payload = {"baseline_balance": round(float(balance), 2),
                       "started_at": int(self._clock())}
            _atomic_write(self.cfg.state_path, payload)
            log.warning("loss guard baseline saldo %.2f — hard stop di equity %.2f",
                        payload["baseline_balance"],
                        payload["baseline_balance"] - self.cfg.limit_usd)
            return payload["baseline_balance"]

    def _log_snapshot(self, account: AccountState, baseline: float,
                      loss: float) -> None:
        """Jejak PnL akun tiap ~60s: tanpa ini angka rugi hanya terlihat saat breach."""
        now = self._clock()
        if now < self._next_log:
            return
        self._next_log = now + 60.0
        log.info("akun: equity=%.2f balance=%.2f baseline=%.2f pnl=%+.2f "
                 "(hard stop -%.2f)", account.equity, account.balance, baseline,
                 -loss, self.cfg.limit_usd)

    # ---- trigger ---------------------------------------------------
    def _trip(self, account: AccountState, baseline: float, loss: float) -> bool:
        # Pagar kedua: walau _trip terpanggil dari tempat yang salah, ia tidak
        # boleh menyentuh posisi sebelum rugi benar-benar >= limit.
        if loss < self.cfg.limit_usd - 1e-9:
            log.error("_trip DIBATALKAN: rugi %.2f < limit %.2f", loss,
                      self.cfg.limit_usd)
            return False
        with _locked(self.cfg.lock_path):
            if self._latch_payload() is not None:      # instance lain menang
                self.locked = True
                return True
            _atomic_write(self.cfg.latch_path, {
                "title": LATCH_TITLE, "ts": int(self._clock()),
                "login": account.login, "equity": round(account.equity, 2),
                "balance": round(account.balance, 2),
                "baseline": round(baseline, 2),
                "loss": round(loss, 2), "limit": self.cfg.limit_usd,
                "triggered_by": f"{self._symbol}:{self._magic}",
            })
        self.locked = True
        log.error("%s: rugi %.2f (equity %.2f, baseline %.2f) — flatten + stop app",
                  LATCH_TITLE, loss, account.equity, baseline)
        result = self._flatten_retry()
        self._cleanup = self._has_positions()
        self._notify(self._message(account, baseline, loss, result))
        if self._cleanup:
            log.error("posisi BELUM tertutup (lihat error di atas) — app TIDAK "
                      "dimatikan; flatten dicoba ulang tiap %ss dan order baru "
                      "diblokir", self.cfg.poll_s)
            return False
        self._stopper()
        return True

    def _retry_cleanup(self) -> bool:
        """Latch aktif tapi flatten belum tuntas: ulangi, jangan biarkan yatim."""
        if not self._cleanup:
            return True
        now = self._clock()
        if now < self._next_poll:
            return False
        self._next_poll = now + self.cfg.poll_s
        result = self._flatten_retry()
        if self._has_positions():
            log.error("flatten ulang gagal: %s", "; ".join(result.errors[:3]) or "-")
            return False
        self._cleanup = False
        log.warning("flatten tuntas setelah latch — app dimatikan sekarang")
        self._notify("✅ <b>Posisi akhirnya tertutup</b> setelah hard stop — "
                     f"{len(result.closed)} posisi. App dimatikan.")
        self._stopper()
        return True

    def _flatten_retry(self) -> FlattenResult:
        """Tiga percobaan: EA bisa menolak saat market tutup / requote."""
        result = self._flatten()
        for _ in range(2):
            if not self._has_positions():
                break
            time.sleep(1.5)
            result = self._flatten()
        return result

    def _has_positions(self) -> bool:
        for symbol, magic in self._targets():
            try:
                if self._link.positions(symbol, magic):
                    return True
            except Mt4Error as exc:
                log.error("cek sisa posisi %s/%s gagal: %s", symbol, magic, exc)
        return False

    def _flatten(self) -> FlattenResult:
        closed: list[tuple[str, int, float]] = []
        cancelled: list[int] = []
        errors: list[str] = []
        for symbol, magic in self._targets():
            try:
                for ticket in self._link.pending_tickets(symbol, magic):
                    self._link.order_delete(ticket)
                    cancelled.append(ticket)
            except Mt4Error as exc:
                errors.append(f"{symbol}/{magic} pending: {exc}")
            try:
                for pos in self._link.positions(symbol, magic):
                    ticket = int(pos["ticket"])
                    lots = float(pos["lots"])
                    self._link.close_position(ticket, lots)
                    closed.append((symbol, ticket, lots))
            except Mt4Error as exc:
                errors.append(f"{symbol}/{magic} posisi: {exc}")
        for line in errors:
            log.error("flatten gagal: %s", line)
        log.warning("flatten selesai: %d posisi ditutup, %d pending dibatalkan, "
                    "%d error", len(closed), len(cancelled), len(errors))
        return FlattenResult(tuple(closed), tuple(cancelled), tuple(errors))

    def _targets(self) -> list[tuple[str, int]]:
        """Instance sendiri + semua simbol di registry (magic sniper & handgun)."""
        pairs = {(self._symbol, self._magic), (self._symbol, self._magic + 1)}
        try:
            raw = json.loads(Path(self.cfg.symbols_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.error("%s tidak ada — flatten hanya simbol sendiri (%s)",
                      self.cfg.symbols_path, self._symbol)
            return sorted(pairs)
        except (OSError, ValueError) as exc:
            log.error("%s rusak (%s) — flatten hanya simbol sendiri (%s)",
                      self.cfg.symbols_path, exc, self._symbol)
            return sorted(pairs)
        for symbol, cfg in raw.items():
            try:
                base = int(cfg["magic"])
            except (KeyError, TypeError, ValueError):
                log.error("registry %s: magic %r tidak valid — dilewati", symbol, cfg)
                continue
            pairs.add((str(symbol).upper(), base))
            pairs.add((str(symbol).upper(), base + 1))
        return sorted(pairs)

    # ---- efek samping ----------------------------------------------
    def _message(self, account: AccountState, baseline: float, loss: float,
                 result: FlattenResult) -> str:
        lines = [
            f"🛑 <b>{LATCH_TITLE} — RUGI ${loss:.2f}</b>",
            f"Equity ${account.equity:.2f} · baseline ${baseline:.2f} · "
            f"limit -${self.cfg.limit_usd:.2f}",
            f"Posisi ditutup: {len(result.closed)} · pending dibatalkan: "
            f"{len(result.cancelled)}",
        ]
        for symbol, ticket, lots in result.closed:
            lines.append(f"• {symbol} #{ticket} {lots:g} lot")
        if result.errors:
            lines.append("⚠️ Gagal sebagian: " + "; ".join(result.errors[:3]))
        lines.append("App dimatikan — tidak ada transaksi baru. Buka kunci manual: "
                     f"<code>rm {self.cfg.latch_path}</code>")
        return "\n".join(lines)

    def _notify(self, text: str) -> None:
        if self._notifier is None:
            log.warning("loss guard (dry-run): %s", text.replace("\n", " | "))
            return
        if not self._notifier.send(text):
            log.error("notifikasi hard stop gagal dikirim")

    def _stop_units(self) -> None:
        if not self.cfg.stop_units:
            log.error("LOSS_GUARD_STOP_UNITS kosong — app ini berhenti sendiri, "
                      "instance lain tidak dimatikan")
            return
        cmd = [self.cfg.systemctl, "--user", "stop", "--no-block",
               *self.cfg.stop_units]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("systemctl stop gagal: %s", exc)
            return
        if done.returncode != 0:
            log.error("systemctl stop rc=%d: %s", done.returncode,
                      (done.stderr or done.stdout).strip()[:400])

    def _latch_payload(self) -> dict | None:
        return _read_json(self.cfg.latch_path)
