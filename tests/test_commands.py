"""Tests for Telegram command parsing (security-relevant: chat gating)."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.commands import parse_command  # noqa: E402


def test_parse_plain_and_slash():
    assert parse_command("/status") == "status"
    assert parse_command("status") == "status"
    assert parse_command("/planning") == "planning"
    assert parse_command("planning") == "planning"


def test_parse_group_suffix_and_case():
    assert parse_command("/status@sebas_scalper_bot") == "status"
    assert parse_command("/PLANNING") == "planning"


def test_parse_unknown_and_empty():
    assert parse_command("/unknown") == ""
    assert parse_command("halo semua") == ""
    assert parse_command("") == ""
    assert parse_command("   ") == ""


def test_parse_start_help():
    assert parse_command("/start") == "start"
    assert parse_command("/help") == "help"
