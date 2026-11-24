"""Tests for fail-loudly configuration (arming layers, ranges, contradictions)."""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.config import load_settings  # noqa: E402

KEYS = ("ORDER_ENABLED", "MT4_ALLOW_TRADING", "RISK_PERCENT", "MAX_LOT",
        "MT4_BRIDGE_PORT", "MT4_MAGIC", "ADVISOR_URL", "ADVISOR_MODEL",
        "MT4_SYMBOL", "ORDER_DEDUPE_SECONDS", "ADVISOR_BAND_ATR",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SYMBOL",
        "LOSS_LIMIT_USD", "LOSS_GUARD_STOP_UNITS", "LOSS_GUARD_POLL_S",
        "LOSS_GUARD_CONFIRM")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_read_only(monkeypatch):
    settings = load_settings("")
    assert settings.orders.enabled is False
    assert settings.orders.armed is False
    assert settings.orders.port == 8770
    assert settings.orders.symbol == "XAUUSD"
    assert settings.orders.advisor_on is False


def test_enabling_orders_requires_matching_gate(monkeypatch):
    monkeypatch.setenv("ORDER_ENABLED", "1")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "MT4_ALLOW_TRADING" in str(exc.value)


def test_allow_trading_alone_is_rejected(monkeypatch):
    monkeypatch.setenv("MT4_ALLOW_TRADING", "1")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "ORDER_ENABLED" in str(exc.value)


def test_both_gates_on_is_accepted(monkeypatch):
    monkeypatch.setenv("ORDER_ENABLED", "1")
    monkeypatch.setenv("MT4_ALLOW_TRADING", "1")
    settings = load_settings("")
    assert settings.orders.armed is True


@pytest.mark.parametrize("key,value", [
    ("RISK_PERCENT", "0"), ("RISK_PERCENT", "9"), ("MAX_LOT", "0"),
    ("MT4_BRIDGE_PORT", "0"), ("MT4_BRIDGE_PORT", "70000"), ("MT4_MAGIC", "0"),
    ("ORDER_DEDUPE_SECONDS", "0"), ("ADVISOR_BAND_ATR", "-1"),
    ("RISK_PERCENT", "abc"),
])
def test_invalid_numbers_fail_loudly(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        load_settings("")


def test_advisor_url_requires_model(monkeypatch):
    monkeypatch.setenv("ADVISOR_URL", "http://localhost:11434/v1")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "ADVISOR_MODEL" in str(exc.value)


def test_mt4_symbol_defaults_to_feed_symbol(monkeypatch):
    monkeypatch.setenv("SYMBOL", "golddec")
    settings = load_settings("")
    assert settings.symbol == "GOLDDEC"
    assert settings.orders.symbol == "GOLDDEC"


def test_half_telegram_config_fails_loudly(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    with pytest.raises(SystemExit):
        load_settings("")


@pytest.mark.parametrize("key", ["WORKER_GRPC_ADDR", "TICK_SOURCE", "SESSION_ID",
                                 "MARKET_DEPTH", "BROKER_TICKS", "FALLBACK_AFTER_S"])
def test_legacy_worker_env_is_rejected(monkeypatch, key):
    """Tick hanya dari MT4 — var feed kantor yang tersisa = konfigurasi basi."""
    monkeypatch.setenv(key, "x")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert key in str(exc.value)


def test_loss_guard_is_off_by_default(monkeypatch):
    settings = load_settings("")
    assert settings.risk_guard.limit_usd == 0.0
    assert settings.risk_guard.stop_units == ()


def test_loss_guard_requires_stop_units(monkeypatch):
    monkeypatch.setenv("LOSS_LIMIT_USD", "8")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "LOSS_GUARD_STOP_UNITS" in str(exc.value)


def test_loss_guard_settings_are_parsed(monkeypatch):
    monkeypatch.setenv("LOSS_LIMIT_USD", "8")
    monkeypatch.setenv("LOSS_GUARD_STOP_UNITS", "a.service, b.service")
    monkeypatch.setenv("LOSS_GUARD_CONFIRM", "3")
    settings = load_settings("")
    assert settings.risk_guard.limit_usd == 8.0
    assert settings.risk_guard.confirm == 3
    assert settings.risk_guard.stop_units == ("a.service", "b.service")


@pytest.mark.parametrize("key,value", [
    ("LOSS_LIMIT_USD", "-1"), ("LOSS_GUARD_POLL_S", "0"),
    ("LOSS_GUARD_CONFIRM", "0"), ("LOSS_GUARD_CONFIRM", "99"),
])
def test_loss_guard_invalid_numbers_fail_loudly(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        load_settings("")
