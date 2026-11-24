"""Control API monitor: status, report, dan ganti simbol/layering (loopback)."""
import json
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.control import ControlServer  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"symbol": "XAUUSD", "price": 3900.5}

    def report(self, kind):
        return f"laporan {kind}"

    def set_symbol(self, symbol):
        self.calls.append(symbol)
        if symbol == "GOLDI":
            return False, "GOLDI tidak tersedia di broker: bad_symbol"
        return True, f"perpindahan ke {symbol} diproses"

    def set_layering(self, enabled):
        self.calls.append(f"layering:{enabled}")
        return True, f"layering {'ON' if enabled else 'OFF'}"


@pytest.fixture
def server():
    runner = FakeRunner()
    srv = ControlServer("127.0.0.1", 0, runner.status,
                        runner.report, runner.set_symbol, runner.set_layering)
    assert srv.start()
    yield srv, runner
    srv.stop()


def _get(addr, path):
    url = f"http://127.0.0.1:{addr[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def _post(addr, path, payload, headers=None, ctype="application/json"):
    url = f"http://127.0.0.1:{addr[1]}{path}"
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": ctype}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_health_and_status(server):
    srv, _ = server
    code, data = _get(srv.addr, "/api/health")
    assert code == 200 and data["ok"] is True
    code, data = _get(srv.addr, "/api/status")
    assert code == 200 and data["price"] == 3900.5


def test_report_endpoint(server):
    srv, _ = server
    code, data = _get(srv.addr, "/api/report?kind=planning")
    assert code == 200 and data["text"] == "laporan planning"
    code, _ = _get(srv.addr, "/api/report?kind=hack")
    assert code == 400


def test_source_endpoint_removed(server):
    """Feed kantor dipensiunkan — endpoint /api/source harus hilang total."""
    srv, runner = server
    code, _ = _post(srv.addr, "/api/source", {"mode": "office"},
                    headers={"X-Xau-Control": "1"})
    assert code == 404
    assert runner.calls == []


def test_layering_toggle(server):
    srv, runner = server
    code, data = _post(srv.addr, "/api/layering", {"enabled": True},
                       headers={"X-Xau-Control": "1"})
    assert code == 200 and data["ok"] is True
    assert runner.calls == ["layering:True"]
    code, data = _post(srv.addr, "/api/layering", {"enabled": "yes"},
                       headers={"X-Xau-Control": "1"})
    assert code == 400


def test_unknown_path_404(server):
    srv, _ = server
    code, data = _get(srv.addr, "/api/nope")
    assert code == 404
    assert data["error"] == "not found"


def test_symbol_switch_applies(server):
    srv, runner = server
    code, data = _post(srv.addr, "/api/symbol", {"symbol": "EURUSD"},
                       headers={"X-Xau-Control": "1"})
    assert code == 200 and data["ok"] is True
    assert runner.calls == ["EURUSD"]


def test_symbol_switch_rejection_is_409(server):
    srv, _ = server
    code, data = _post(srv.addr, "/api/symbol", {"symbol": "GOLDI"},
                       headers={"X-Xau-Control": "1"})
    assert code == 409 and data["ok"] is False


def test_symbol_requires_string_field(server):
    srv, runner = server
    code, data = _post(srv.addr, "/api/symbol", {"symbol": 42},
                       headers={"X-Xau-Control": "1"})
    assert code == 400 and "'symbol'" in data["error"]
    assert runner.calls == []


def test_symbol_endpoint_requires_guard_header(server):
    srv, runner = server
    code, _ = _post(srv.addr, "/api/symbol", {"symbol": "EURUSD"})
    assert code == 403
    assert runner.calls == []


def test_foreign_host_header_blocked(server):
    srv, _ = server
    url = f"http://127.0.0.1:{srv.addr[1]}/api/status"
    req = urllib.request.Request(url, headers={"Host": "evil.example"})
    try:
        urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    else:                                       # pragma: no cover
        raise AssertionError("Host asing harus ditolak")
