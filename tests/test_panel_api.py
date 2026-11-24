"""API panel: state, kontrol monitor, file, log, dan penyajian aset statis."""
import json
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.panel.files import FileRegistry  # noqa: E402
from xau_monitor.panel.logs import LogTail  # noqa: E402
from xau_monitor.panel.server import PanelApp, PanelServer  # noqa: E402
from xau_monitor.panel.targets import Target  # noqa: E402


class FakeSupervisor:
    def __init__(self):
        self.calls = []

    def state(self):
        return {"running": False, "pid": None}

    def start(self):
        self.calls.append("start")
        return {"ok": True, "message": "monitor dimulai", "monitor": self.state()}

    def stop(self):
        self.calls.append("stop")
        return {"ok": True, "message": "monitor berhenti", "monitor": self.state()}

    def restart(self):
        self.calls.append("restart")
        return {"ok": True, "message": "restart selesai", "monitor": self.state()}


class FakeControl:
    def __init__(self, symbol="XAUUSD"):
        self.symbol = symbol
        self.symbols = []

    def status(self):
        return {"symbol": self.symbol, "price": 3901.25,
                "tick": {"source": "mt4"}}, ""

    def report(self, kind):
        return f"<b>laporan</b> {kind} {self.symbol}", ""

    def set_symbol(self, symbol):
        self.symbols.append(symbol)
        return {"ok": symbol != "GOLDI", "message": f"simbol {symbol}"}, ""


@pytest.fixture
def panel(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("SYMBOL=XAUUSD\nTELEGRAM_BOT_TOKEN=abc123\n", encoding="utf-8")
    (root / "README.md").write_text("# xau\n", encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    (logs / "monitor.log").write_text("2026-11-16 INFO boot\n2026-11-16 ERROR gagal\n",
                                      encoding="utf-8")
    sup = FakeSupervisor()
    ctl = FakeControl("XAUUSD")
    ctl2 = FakeControl("EURUSD")
    (logs / "eurusd.log").write_text("2026-11-17 INFO eur seed\n",
                                     encoding="utf-8")
    targets = {
        "XAUUSD": Target("XAUUSD", ctl, LogTail(logs / "monitor.log")),
        "EURUSD": Target("EURUSD", ctl2, LogTail(logs / "eurusd.log")),
    }
    app = PanelApp(sup, FileRegistry(root), LogTail(logs / "stdio.log"),
                   targets, "XAUUSD")
    httpd = PanelServer(("127.0.0.1", 0), app)
    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[:2], root, sup, ctl
    httpd.shutdown()
    httpd.server_close()


def _call(addr, path, method="GET", payload=None, guard=True):
    url = f"http://127.0.0.1:{addr[1]}{path}"
    headers = {"X-Xau-Panel": "1"} if guard else {}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _json(raw):
    return json.loads(raw.decode())


def test_state_combines_supervisor_and_control(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/state", guard=False)
    data = _json(raw)
    assert code == 200
    assert data["monitor"]["running"] is False
    assert data["status"]["price"] == 3901.25
    assert data["control"]["ok"] is True


def test_monitor_actions_need_guard(panel):
    addr, _root, sup, _ = panel
    code, _, _ = _call(addr, "/api/monitor/restart", method="POST", payload={}, guard=False)
    assert code == 403
    assert sup.calls == []

    code, _, raw = _call(addr, "/api/monitor/restart", method="POST", payload={})
    assert code == 200 and _json(raw)["ok"] is True
    assert sup.calls == ["restart"]


def test_symbol_proxy_status_codes(panel):
    addr, _root, _sup, ctl = panel
    code, _, raw = _call(addr, "/api/symbol", method="POST",
                         payload={"symbol": "EURUSD"})
    assert code == 200 and _json(raw)["ok"] is True
    code, _, raw = _call(addr, "/api/symbol", method="POST",
                         payload={"symbol": "GOLDI"})
    assert code == 409 and _json(raw)["ok"] is False
    code, _, raw = _call(addr, "/api/symbol", method="POST", payload={"symbol": 123})
    assert code == 400
    assert ctl.symbols == ["EURUSD", "GOLDI"]


def test_symbols_lists_registry(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/symbols", guard=False)
    data = _json(raw)
    assert code == 200
    assert data["default"] == "XAUUSD"
    assert data["instances"] == ["XAUUSD", "EURUSD"]


def test_instance_param_routes_to_right_monitor(panel):
    addr, _root, _sup, _ctl = panel
    code, _, raw = _call(addr, "/api/state?instance=EURUSD", guard=False)
    assert code == 200 and _json(raw)["status"]["symbol"] == "EURUSD"
    code, _, raw = _call(addr, "/api/report?kind=status&instance=eurusd", guard=False)
    assert "EURUSD" in _json(raw)["text"]


def test_unknown_instance_is_404(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/state?instance=NOPE", guard=False)
    assert code == 404 and "NOPE" in _json(raw)["error"]
    code, _, raw = _call(addr, "/api/symbol?instance=NOPE", method="POST",
                         payload={"symbol": "EURUSD"})
    assert code == 404


def test_instance_logs_use_own_file(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/logs?instance=EURUSD&limit=10", guard=False)
    data = _json(raw)
    assert code == 200
    assert any("eur seed" in line for line in data["lines"])


def test_read_masks_env_secret(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/file?id=env", guard=False)
    data = _json(raw)
    assert code == 200
    assert "abc123" not in data["content"]
    assert "__KEEP_SECRET__" in data["content"]


def test_put_file_preserves_secret(panel):
    addr, root, *_ = panel
    _, _, raw = _call(addr, "/api/file?id=env", guard=False)
    current = _json(raw)
    code, _, raw = _call(addr, "/api/file", method="PUT",
                         payload={"id": "env", "content":
                                  "SYMBOL=GOLD\nTELEGRAM_BOT_TOKEN=__KEEP_SECRET__\n",
                                  "mtime_ns": current["mtime_ns"]})
    assert code == 200, raw
    text = (root / ".env").read_text(encoding="utf-8")
    assert "SYMBOL=GOLD" in text and "TELEGRAM_BOT_TOKEN=abc123" in text


def test_unknown_file_is_404(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/file?id=../../etc/passwd", guard=False)
    assert code == 404 and "tidak dikenal" in _json(raw)["error"]


def test_logs_tail(panel):
    addr, *_ = panel
    code, _, raw = _call(addr, "/api/logs?source=monitor&limit=10", guard=False)
    data = _json(raw)
    assert code == 200
    assert data["lines"][-1].endswith("ERROR gagal")


def test_static_index_served_with_csp(panel):
    addr, *_ = panel
    code, headers, raw = _call(addr, "/", guard=False)
    assert code == 200
    assert b"XAU Monitor" in raw
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
