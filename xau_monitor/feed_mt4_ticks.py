"""Tick/candle dari MT4 sebagai sumber PRIMER dan SATU-SATUNYA.

Mengambil bar M1 native broker berarti memakai agregasi tick yang dilakukan
terminal sendiri. Sampling quote (mis. 2 Hz) akan kehilangan wick intra-detik;
karena itu tiap polling kita suntikkan **high, low, close** bar M1 berjalan ke
builder. Hasilnya OHLC M1 identik dengan yang dihitung broker, berapa pun
frekuensi polling-nya.

Thread ini hanya melakukan I/O; pembentukan candle tetap di `M1Builder`.
"""
from __future__ import annotations

import logging
import threading
import time

from .mt4_link import Mt4Error, Mt4Link
from .mt4_wire import as_int

log = logging.getLogger("mt4ticks")


class Mt4BarTicks(threading.Thread):
    def __init__(self, link: Mt4Link, symbol: str, outq, poll_ms: int = 500) -> None:
        super().__init__(name="mt4-m1", daemon=True)
        self._link = link
        self._symbol = symbol
        self._outq = outq
        self._poll_s = max(poll_ms, 100) / 1000.0
        self._stop = threading.Event()
        self.tick_count = 0
        self.last_tick = 0.0
        self.last_error = ""
        self.last_bar_ts = 0

    @property
    def age(self) -> float:
        return time.time() - self.last_tick if self.last_tick else 1e9

    def run(self) -> None:
        err_logged = 0.0
        while not self._stop.is_set():
            try:
                resp = self._link.call("rates", sym=self._symbol, tf="M1", count=2,
                                       forming=1, timeout=8.0)
                self._emit(resp)
            except (Mt4Error, ValueError, KeyError, IndexError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                now = time.time()
                if now - err_logged > 60:
                    err_logged = now
                    log.warning("bar M1 MT4 gagal: %s", exc)
            self._stop.wait(self._poll_s)
        log.info("feed MT4 berhenti (%d tick, %d bar)", self.tick_count, self.last_bar_ts)

    def _emit(self, resp: dict[str, str]) -> None:
        offset = as_int(resp, "gmt", 0)
        records = [r for r in resp.get("bars", "").split("~") if r]
        if not records:
            return
        parts = records[-1].split(",")
        if len(parts) != 6:
            raise ValueError(f"bar M1 rusak: {records[-1][:60]!r}")
        bar_ts = int(parts[0]) - offset          # detik, sudah UTC
        high, low, close = float(parts[2]), float(parts[3]), float(parts[4])
        if not (low <= close <= high) or close <= 0:
            raise ValueError(f"bar M1 tidak wajar: {records[-1][:60]!r}")
        # Urutan penting: high & low dulu supaya builder mencatat wick broker,
        # close terakhir supaya candle berakhir di harga terakhir.
        ts_ms = (bar_ts + 1) * 1000
        for price in (high, low, close):
            self._outq.put((self._symbol, ts_ms, price))
            self.tick_count += 1
        self.last_tick = time.time()
        self.last_bar_ts = bar_ts
        self.last_error = ""     # error lama tidak boleh menetap setelah pulih

    def stop(self) -> None:
        self._stop.set()
