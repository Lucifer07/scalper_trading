"""Tests for the MT4 pipe protocol codec (no sockets, no MT4)."""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.mt4_wire import (  # noqa: E402
    WireError, as_float, as_int, clean, encode_request, encode_response, fmt,
    ok_of, parse_request, parse_response,
)


def test_encode_request_layout():
    raw = encode_request(7, "order_send", side="sell", lots=0.1, sl=2005.5)
    assert raw.endswith(b"\n")
    assert raw.decode() == "7|order_send|side=sell;lots=0.1;sl=2005.5\n"


def test_encode_request_without_params():
    assert encode_request(1, "ping").decode() == "1|ping\n"


def test_round_trip_request():
    raw = encode_request(12, "symbol_info", sym="XAUUSD").decode().strip()
    req_id, cmd, params = parse_request(raw)
    assert (req_id, cmd, params) == (12, "symbol_info", {"sym": "XAUUSD"})


def test_round_trip_response_ok_and_error():
    ok_line = encode_response(3, True, ticket=9001).decode().strip()
    req_id, params = parse_response(ok_line)
    assert req_id == 3 and ok_of(params) and params["ticket"] == "9001"

    err_line = encode_response(4, False, err="spread", msg="wide").decode().strip()
    req_id, params = parse_response(err_line)
    assert req_id == 4 and not ok_of(params)
    assert params["err"] == "spread" and params["msg"] == "wide"


@pytest.mark.parametrize("bad_id", [0, -5])
def test_request_rejects_bad_id(bad_id):
    with pytest.raises(WireError):
        encode_request(bad_id, "ping")


@pytest.mark.parametrize("cmd", ["PING", "order-send", "", "x" * 40])
def test_request_rejects_bad_cmd(cmd):
    with pytest.raises(WireError):
        encode_request(1, cmd)


def test_fmt_rejects_nan_and_inf():
    with pytest.raises(WireError):
        fmt(float("nan"))
    with pytest.raises(WireError):
        fmt(float("inf"))


def test_clean_strips_framing_characters():
    assert clean("a|b;c\nd\re") == "a_b_c_d_e"
    assert fmt(True) == "1" and fmt(False) == "0"


def test_parse_response_rejects_malformed():
    for line in ("", "x|ok=1", "5|ticket=1", "5|ok=2", "5|ok=1||"):
        with pytest.raises(WireError):
            parse_response(line)


def test_parse_request_rejects_malformed():
    for line in ("1", "1|PING", "1|ping|novalue"):
        with pytest.raises(WireError):
            parse_request(line)


def test_numeric_helpers():
    params = {"a": "12", "b": "2.5", "empty": ""}
    assert as_int(params, "a") == 12
    assert as_float(params, "b") == 2.5
    assert as_int(params, "empty", 3) == 3
    with pytest.raises(WireError):
        as_int(params, "b")
    with pytest.raises(WireError):
        as_int(params, "missing")
