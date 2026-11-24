"""Tests for the MT4 MCP server (JSON-RPC in-process, no stdio spawn)."""
import json
import sys
import time

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from tools.fake_ea import FakeEa  # noqa: E402
from tools.mcp_mt4 import McpServer  # noqa: E402
from xau_monitor.mt4_link import Mt4Link  # noqa: E402


@pytest.fixture
def rig():
    link = Mt4Link(0, call_timeout=1.0)
    ea = FakeEa(link.port).start()
    deadline = time.time() + 3.0
    while not link.connected and time.time() < deadline:
        time.sleep(0.02)
    yield link, ea
    ea.stop()
    link.shutdown()


def rpc(server: McpServer, method: str, params: dict | None = None, rid=1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params or {}})


def test_initialize_and_ping(rig):
    link, _ea = rig
    server = McpServer(link, "XAUUSD", 20261116, allow_trading=False)
    init = rpc(server, "initialize")["result"]
    assert init["serverInfo"]["name"] == "mt4-bridge"
    assert rpc(server, "ping")["result"] == {}
    assert server.handle({"method": "notifications/initialized"}) is None


def test_unknown_method_is_error(rig):
    link, _ea = rig
    resp = rpc(McpServer(link, "XAUUSD", 1, False), "tools/nope")
    assert resp["error"]["code"] == -32601


def test_read_only_tools_exclude_order(rig):
    link, _ea = rig
    server = McpServer(link, "XAUUSD", 20261116, allow_trading=False)
    names = [t["name"] for t in rpc(server, "tools/list")["result"]["tools"]]
    assert "mt4_quote" in names and "mt4_order" not in names

    armed = McpServer(link, "XAUUSD", 20261116, allow_trading=True)
    names = [t["name"] for t in rpc(armed, "tools/list")["result"]["tools"]]
    assert "mt4_order" in names and "mt4_close" in names


def test_quote_and_symbol_tools(rig):
    link, _ea = rig
    server = McpServer(link, "XAUUSD", 20261116, False)
    quote = json.loads(rpc(server, "tools/call", {"name": "mt4_quote",
                                                  "arguments": {}})["result"]["content"][0]["text"])
    assert quote["sym"] == "XAUUSD"
    assert float(quote["bid"]) == pytest.approx(2000.0)

    spec = json.loads(rpc(server, "tools/call", {"name": "mt4_symbol",
                                                 "arguments": {"symbol": "xauusd"}})["result"]["content"][0]["text"])
    assert spec["symbol"] == "XAUUSD"
    assert spec["lot_min"] == pytest.approx(0.01)


def test_positions_tool(rig):
    link, ea = rig
    server = McpServer(link, ea.magic, ea.magic, False)
    empty = rpc(server, "tools/call", {"name": "mt4_positions",
                                       "arguments": {"symbol": "XAUUSD"}})["result"]
    assert json.loads(empty["content"][0]["text"]) == []


def test_order_tool_gated_without_flag(rig):
    link, _ea = rig
    server = McpServer(link, "XAUUSD", 20261116, allow_trading=False)
    resp = rpc(server, "tools/call", {"name": "mt4_order", "arguments": {
        "side": "sell", "symbol": "XAUUSD", "lots": 0.1, "sl": 2005.0, "tp": 1990.0}})
    assert resp["result"]["isError"] is True
    assert "--allow-trading" in resp["result"]["content"][0]["text"]


def test_order_tool_validates_geometry_and_places_order(rig):
    link, ea = rig
    server = McpServer(link, "XAUUSD", ea.magic, allow_trading=True)
    bad = rpc(server, "tools/call", {"name": "mt4_order", "arguments": {
        "side": "sell", "symbol": "XAUUSD", "lots": 0.1, "sl": 1990.0, "tp": 2005.0}})
    assert bad["result"]["isError"] is True
    assert "geometri" in bad["result"]["content"][0]["text"]

    good = rpc(server, "tools/call", {"name": "mt4_order", "arguments": {
        "side": "sell", "symbol": "XAUUSD", "lots": 0.1, "sl": 2005.0, "tp": 1990.0}})
    assert "isError" not in good["result"]
    assert ea.sent_orders[0]["side"] == "sell"


def test_close_tool_reports_unknown_ticket(rig):
    link, ea = rig
    server = McpServer(link, "XAUUSD", ea.magic, allow_trading=True)
    resp = rpc(server, "tools/call", {"name": "mt4_close",
                                      "arguments": {"ticket": 123456}})
    assert resp["result"]["isError"] is True
    assert "ticket_not_found" in resp["result"]["content"][0]["text"]
