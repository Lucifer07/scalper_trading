"""Argumen CLI panel untuk deployment multi-instance (satu simbol per proses)."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.panel.__main__ import _parse_args  # noqa: E402


def test_log_file_default_falls_back_to_dotenv():
    assert _parse_args([]).log_file is None


def test_log_file_override():
    assert _parse_args(["--log-file", "logs/eurusd.log"]).log_file == "logs/eurusd.log"


def test_per_symbol_ports_are_independent():
    args = _parse_args(["--port", "8782", "--control-port", "8781"])
    assert args.port == 8782
    assert args.control_port == 8781
