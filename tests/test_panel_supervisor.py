"""Supervisor child process: start/stop/restart dan pelacakan exit code."""
import sys
import time

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.panel.supervisor import Supervisor  # noqa: E402


def test_start_stop_roundtrip(tmp_path):
    (tmp_path / "sleepy.py").write_text("import time\nwhile True:\n    time.sleep(1)\n",
                                        encoding="utf-8")
    sup = Supervisor(tmp_path, log_dir=tmp_path / "logs", python=sys.executable,
                     module="sleepy")
    assert sup.alive() is False
    result = sup.start()
    assert result["ok"] is True
    assert result["monitor"]["pid"] and result["monitor"]["running"] is True
    assert sup.alive() is True

    stopped = sup.stop()
    assert stopped["monitor"]["running"] is False
    assert stopped["message"].startswith("monitor berhenti")


def test_crashing_child_records_exit_code(tmp_path):
    sup = Supervisor(tmp_path, log_dir=tmp_path / "logs", python=sys.executable,
                     module="no_such_module_xau_test")
    sup.start()
    deadline = time.time() + 10
    while time.time() < deadline and sup.alive():
        time.sleep(0.1)
    state = sup.state()
    assert state["running"] is False
    assert state["returncode"] not in (None, 0)


def test_stop_when_idle_is_safe(tmp_path):
    sup = Supervisor(tmp_path, log_dir=tmp_path / "logs")
    assert sup.stop()["message"] == "monitor tidak berjalan"
    assert sup.state()["running"] is False
