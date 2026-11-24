"""Dead-market guard: deteksi feed beku (bar M1 datar) + jeda evaluasi.

Kasus nyata: XBRUSD HOLD 22-11 04:30 WIB saat market tutup — feed demo tetap
mengirim bar M1 datar 90.26 sepanjang akhir pekan, window 20-bar M15 bergeser
sendiri, dan HOLD palsu terkirim tiap 30 menit.
"""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.candles import Candle  # noqa: E402
from xau_monitor.config import load_settings  # noqa: E402
from xau_monitor.pipeline import market_dead  # noqa: E402
from xau_monitor.report import fmt_market_guard  # noqa: E402
from xau_monitor.runner import MonitorRunner  # noqa: E402

ENV_KEYS = ("ORDER_ENABLED", "MT4_ALLOW_TRADING", "RISK_PERCENT", "MAX_LOT",
            "MT4_BRIDGE_PORT", "MT4_MAGIC", "ADVISOR_URL", "ADVISOR_MODEL",
            "MT4_SYMBOL", "ORDER_DEDUPE_SECONDS", "ADVISOR_BAND_ATR",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SYMBOL", "MODES",
            "DEAD_MARKET_GUARD", "DEAD_MARKET_FLAT_BARS", "CONTROL_PORT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _flat(ts: int, px: float) -> Candle:
    return Candle(ts, px, px, px, px, 3)


def _move(ts: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(ts, o, h, low, c, 10)


class TestMarketDead:
    def test_frozen_feed_detected(self):
        bars = [_flat(60 * i, 90.26) for i in range(6)]
        assert market_dead(bars, 90.26, flat_bars=5) is True

    def test_frozen_real_bar_repeated_detected(self):
        """Kasus nyata XBRUSD 22-11: saat market tutup EA mengembalikan bar
        Jumat terakhir (high/low ada rentang) tiap poll — bar baru per menit
        dengan OHLC identik. Ini HARUS dianggap beku."""
        real = Candle(1795296000, 90.26, 90.44, 90.18, 90.26, 40)
        bars = [Candle(1795296000 + 60 * i, real.o, real.h, real.l, real.c, 40)
                for i in range(6)]
        assert market_dead(bars, 90.26, flat_bars=5) is True

    def test_repeated_bar_but_price_moves_releases(self):
        real = Candle(0, 90.26, 90.44, 90.18, 90.26, 40)
        bars = [Candle(60 * i, real.o, real.h, real.l, real.c, 40)
                for i in range(6)]
        assert market_dead(bars, 90.30, flat_bars=5) is False

    def test_insufficient_bars_means_alive(self):
        bars = [_flat(60 * i, 90.26) for i in range(3)]
        assert market_dead(bars, 90.26, flat_bars=5) is False

    def test_one_moving_bar_releases(self):
        bars = ([_flat(60 * i, 90.26) for i in range(5)]
                + [_move(300, 90.26, 90.31, 90.20, 90.30)])
        assert market_dead(bars, 90.30, flat_bars=5) is False

    def test_live_price_differs_releases_instantly(self):
        """Tick pertama setelah beku (harga != close terakhir) lepaskan guard,
        tidak menunggu bar bergerak menutup."""
        bars = [_flat(60 * i, 90.26) for i in range(6)]
        assert market_dead(bars, 90.27, flat_bars=5) is False

    def test_flat_bars_at_different_levels_stay_alive(self):
        """Doji tanpa rentang di level harga berbeda = market sepi, bukan beku."""
        bars = [_flat(60 * i, 90.20 + 0.01 * i) for i in range(6)]
        assert market_dead(bars, bars[-1].c, flat_bars=5) is False

    def test_flat_bars_below_two_never_dead(self):
        bars = [_flat(0, 90.26), _flat(60, 90.26)]
        assert market_dead(bars, 90.26, flat_bars=1) is False


class TestMarketGuardConfig:
    def test_defaults_guard_on_flat_five(self):
        s = load_settings("")
        assert s.dead_market_guard is True
        assert s.dead_market_flat_bars == 5

    def test_flat_bars_below_two_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("DEAD_MARKET_FLAT_BARS", "1")
        with pytest.raises(SystemExit) as exc:
            load_settings("")
        assert "DEAD_MARKET_FLAT_BARS" in str(exc.value)

    def test_non_numeric_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("DEAD_MARKET_FLAT_BARS", "abc")
        with pytest.raises(SystemExit):
            load_settings("")

    def test_guard_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("DEAD_MARKET_GUARD", "0")
        assert load_settings("").dead_market_guard is False


class TestMarketGuardWiring:
    """Runner: feed beku -> evaluasi dijeda + 1 notifikasi; harga bergerak ->
    lanjut + 1 notifikasi pulih. Tidak ada pengulangan notifikasi."""

    @staticmethod
    def _feed_minute(runner: MonitorRunner, minute: int, px: float) -> None:
        runner.builder.add_tick(minute * 60_000 + 1_000, px)
        runner.price = px

    def _freeze(self, runner: MonitorRunner, minutes: int = 6) -> None:
        for m in range(minutes):
            self._feed_minute(runner, m, 90.26)

    def test_freeze_pauses_and_notifies_once(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr("xau_monitor.runner.deliver",
                            lambda notifier, text: sent.append(text))
        runner = MonitorRunner(load_settings(""))
        self._freeze(runner)
        runner._on_m1_close()
        assert runner._market_dead_prev is True
        assert len(sent) == 1
        assert "tutup/datar" in sent[0]

        # Menit berikutnya tetap beku: tidak ada notifikasi/evaluasi ulang.
        self._feed_minute(runner, 6, 90.26)
        runner._on_m1_close()
        assert len(sent) == 1
        assert runner.snaps == {}      # evaluasi memang dilewati

    def test_movement_resumes_and_notifies_once(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr("xau_monitor.runner.deliver",
                            lambda notifier, text: sent.append(text))
        runner = MonitorRunner(load_settings(""))
        self._freeze(runner)
        runner._on_m1_close()                 # masuk jeda
        self._feed_minute(runner, 6, 90.31)   # tick pertama bergerak
        runner._on_m1_close()
        assert runner._market_dead_prev is False
        assert len(sent) == 2
        assert "aktif kembali" in sent[1]
        assert "M1" in runner.snaps            # evaluasi kembali berjalan

    def test_guard_disabled_evaluates_frozen_data(self, monkeypatch):
        monkeypatch.setenv("DEAD_MARKET_GUARD", "0")
        sent: list[str] = []
        monkeypatch.setattr("xau_monitor.runner.deliver",
                            lambda notifier, text: sent.append(text))
        runner = MonitorRunner(load_settings(""))
        self._freeze(runner)
        runner._on_m1_close()
        assert sent == []
        assert "M1" in runner.snaps            # perilaku lama: tetap evaluasi


def test_fmt_market_guard_messages():
    dead = fmt_market_guard("XBRUSD", True, 5, 90.26, digits=2)
    assert "XBRUSD" in dead and "tutup/datar" in dead and "90.26" in dead
    alive = fmt_market_guard("XBRUSD", False, 5, 90.31, digits=2)
    assert "aktif kembali" in alive and "90.31" in alive
