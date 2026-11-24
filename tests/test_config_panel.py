"""Config panel: default loopback, tolak host non-loopback, port bentrok."""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.config import load_settings  # noqa: E402

KEYS = ("CONTROL_ENABLED", "CONTROL_HOST", "CONTROL_PORT", "LOG_FILE", "COMMANDS_ENABLED",
        "LOG_MAX_BYTES", "LOG_BACKUPS", "MT4_BRIDGE_PORT", "ORDER_ENABLED",
        "MT4_ALLOW_TRADING", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_panel_defaults():
    panel = load_settings("").panel
    assert panel.control_enabled is True
    assert panel.control_host == "127.0.0.1"
    assert panel.control_port == 8772
    assert panel.log_file == "logs/monitor.log"
    assert panel.log_backups == 5


def test_non_loopback_control_host_rejected(monkeypatch):
    monkeypatch.setenv("CONTROL_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "127.0.0.1" in str(exc.value)


def test_control_port_collision_with_bridge_rejected(monkeypatch):
    monkeypatch.setenv("CONTROL_PORT", "8770")
    monkeypatch.setenv("MT4_BRIDGE_PORT", "8770")
    with pytest.raises(SystemExit) as exc:
        load_settings("")
    assert "CONTROL_PORT" in str(exc.value)


@pytest.mark.parametrize("key,value", [
    ("LOG_MAX_BYTES", "10"), ("LOG_BACKUPS", "-1"), ("CONTROL_PORT", "70000"),
    ("CONTROL_PORT", "abc"),
])
def test_invalid_panel_numbers_fail_loudly(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        load_settings("")


def test_control_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CONTROL_ENABLED", "0")
    monkeypatch.setenv("CONTROL_HOST", "0.0.0.0")   # tidak dipakai saat disabled
    assert load_settings("").panel.control_enabled is False


def test_commands_listener_enabled_by_default(monkeypatch):
    monkeypatch.delenv("COMMANDS_ENABLED", raising=False)
    assert load_settings("").commands_enabled is True
    monkeypatch.setenv("COMMANDS_ENABLED", "0")
    assert load_settings("").commands_enabled is False
