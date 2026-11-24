"""Tests for the AI advisor boundary (no network: urlopen is monkeypatched)."""
import io
import json
import sys
import urllib.error

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.advisor import (  # noqa: E402
    AdvisorContext, AdvisorDecision, AdvisorRejected, AdvisorUnavailable,
    HttpAdvisor, NullAdvisor, apply_decision, build_advisor, build_system_prompt,
    CliAdvisor, digest_rules, parse_decision,
)


def ctx(**over) -> AdvisorContext:
    base = dict(symbol="XAUUSD", side="SELL", mode="ENTRY_SELL", price=2000.0,
                zone=2001.0, stop=2005.0, tps=(1990.0, 1980.0), invalid=2010.0,
                atr_m1=1.0, reasons=("D1:DOWN", "jarum M5"))
    return AdvisorContext(**{**base, **over})


class FakeResp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def llm_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_null_advisor_passes_engine_levels():
    decision = NullAdvisor().review(ctx())
    assert decision.action == "approve"
    levels = apply_decision(ctx(), decision, 0.5)
    assert levels.stop == ctx().stop
    assert levels.tps == ctx().tps


def test_parse_decision_approve_and_fenced_json():
    d = parse_decision('```json\n{"action":"approve","confidence":0.9,"rationale":"ok"}\n```')
    assert d.action == "approve" and d.confidence == pytest.approx(0.9)


def test_parse_decision_reject_and_adjust():
    r = parse_decision(json.dumps({"action": "reject", "rationale": "zona kotor"}))
    assert r.action == "reject" and r.rationale == "zona kotor"
    a = parse_decision(json.dumps({"action": "adjust", "sl": 2005.3,
                                   "tps": [1990.2, 1980.2], "confidence": 2}))
    assert a.action == "adjust" and a.sl == pytest.approx(2005.3)
    assert a.tps == (1990.2, 1980.2) and a.confidence == 1.0


def test_parse_decision_rejects_invalid_payloads():
    for content in ("bukan json", "[]", '{"action":"buy"}',
                    '{"action":"adjust","sl":null,"tps":[]}',
                    '{"action":"adjust","sl":"x"}'):
        with pytest.raises(AdvisorUnavailable):
            parse_decision(content)


def test_apply_decision_band_enforced():
    inside = AdvisorDecision("adjust", 2005.4, (1990.2, 1980.2))
    levels = apply_decision(ctx(), inside, 0.5)
    assert levels.stop == pytest.approx(2005.4)
    assert levels.tps == (1990.2, 1980.2)

    outside = AdvisorDecision("adjust", 2007.0, ())
    with pytest.raises(AdvisorRejected):
        apply_decision(ctx(), outside, 0.5)

    bad_tp = AdvisorDecision("adjust", None, (1990.2,))
    with pytest.raises(AdvisorRejected):
        apply_decision(ctx(), bad_tp, 0.5)

    far_tp = AdvisorDecision("adjust", None, (1999.0, 1980.0))
    with pytest.raises(AdvisorRejected):
        apply_decision(ctx(), far_tp, 0.5)


def test_apply_decision_without_atr_cannot_adjust():
    with pytest.raises(AdvisorRejected):
        apply_decision(ctx(atr_m1=0.0), AdvisorDecision("adjust", 2005.1, ()), 0.5)


def test_apply_decision_reject():
    with pytest.raises(AdvisorRejected):
        apply_decision(ctx(), AdvisorDecision("reject", rationale="jangan"), 0.5)


def test_digest_rules_truncates_and_fails_loudly(tmp_path):
    f = tmp_path / "rules.md"
    f.write_text("a" * 100, encoding="utf-8")
    assert digest_rules(str(f), 10).startswith("aaaaaaaaaa")
    assert "dipotong" in digest_rules(str(f), 10)
    with pytest.raises(AdvisorUnavailable):
        digest_rules(str(tmp_path / "missing.md"), 10)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(AdvisorUnavailable):
        digest_rules(str(empty), 10)


def test_system_prompt_contains_hard_constraints():
    prompt = build_system_prompt("RULES", 0.5)
    assert "0.5x ATR" in prompt and "approve|reject|adjust" in prompt
    # Volume diusulkan model, tetapi dinyatakan tegas bahwa guard yang mengunci.
    assert '"lots":number' in prompt
    assert "guard" in prompt and "lot_min" in prompt
    assert "menentukan volume" not in prompt


def test_parse_decision_reads_advisor_lots():
    d = parse_decision(json.dumps({"action": "approve", "lots": 0.07}))
    assert d.lots == pytest.approx(0.07)
    adjust = parse_decision(json.dumps({"action": "adjust", "sl": 2005.3,
                                        "lots": 0.03}))
    assert adjust.lots == pytest.approx(0.03)
    # angka rusak / negatif -> 0 (guard fallback), bukan crash
    for bad in ("abc", -1, None):
        d = parse_decision(json.dumps({"action": "approve", "lots": bad}))
        assert d.lots == 0.0


def test_apply_decision_carries_lots():
    decide = AdvisorDecision("approve", lots=0.04)
    assert apply_decision(ctx(), decide, 0.5).lots == pytest.approx(0.04)
    none = AdvisorDecision("approve")
    assert apply_decision(ctx(), none, 0.5).lots is None


def test_context_facts_expose_lot_bounds():
    facts = ctx(lot=0.05, lot_min=0.01, lot_step=0.01,
                margin_per_lot=700.0).facts()
    assert facts["lot_min"] == 0.01
    assert facts["lot_max_allowed"] == 0.05
    assert facts["lot_step"] == 0.01
    # margin per layer = margin_per_lot x lot_max_allowed (bukan x lots yang dipilih)
    assert facts["margin_per_lot"] == 700.0
    assert facts["margin_per_layer"] == 35.0


def test_http_advisor_round_trip(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return FakeResp(llm_payload('{"action":"approve","confidence":0.8,"rationale":"setuju"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adv = HttpAdvisor("http://localhost:11434/v1", "qwen", "", 3.0, "SYSTEM")
    decision = adv.review(ctx())
    assert decision.action == "approve"
    assert captured["body"]["model"] == "qwen"
    assert captured["body"]["messages"][0]["content"] == "SYSTEM"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["timeout"] == 3.0
    assert json.loads(captured["body"]["messages"][1]["content"])["symbol"] == "XAUUSD"


def test_http_advisor_appends_endpoint_and_sends_auth(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return FakeResp(llm_payload('{"action":"approve"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    HttpAdvisor("http://llm/v1/chat/completions", "m", "secret", 2.0, "S").review(ctx())
    assert seen["url"] == "http://llm/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"


def test_http_advisor_retries_without_json_mode_on_400(monkeypatch):
    calls: list[dict] = []
    state = {"first": True}

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data))
        if state["first"]:
            state["first"] = False
            raise urllib.error.HTTPError(req.full_url, 400, "bad request", {},
                                         io.BytesIO(b'{"error":"unsupported"}'))
        return FakeResp(llm_payload('{"action":"reject","rationale":"tidak"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    decision = HttpAdvisor("http://llm/v1", "m", "", 2.0, "S").review(ctx())
    assert decision.action == "reject"
    assert "response_format" in calls[0] and "response_format" not in calls[1]


def test_http_advisor_network_failure_is_fail_closed(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(AdvisorUnavailable):
        HttpAdvisor("http://llm/v1", "m", "", 2.0, "S").review(ctx())


def test_http_advisor_bad_shape_is_fail_closed(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp({"choices": []}))
    with pytest.raises(AdvisorUnavailable):
        HttpAdvisor("http://llm/v1", "m", "", 2.0, "S").review(ctx())


def test_build_advisor_paths(tmp_path, monkeypatch):
    assert isinstance(build_advisor("", "m", "", 1.0, "x.md", 10, 0.5), NullAdvisor)
    rules = tmp_path / "r.md"
    rules.write_text("aturan", encoding="utf-8")
    adv = build_advisor("http://llm/v1", "m", "k", 1.0, str(rules), 100, 0.5)
    assert isinstance(adv, HttpAdvisor)
    with pytest.raises(AdvisorUnavailable):
        build_advisor("http://llm/v1", "", "k", 1.0, str(rules), 100, 0.5)


# ---- CLI advisor: timeout transient -> retry sekali, lalu fail-closed ----

class FakeProc:
    returncode = 0
    stderr = ""
    stdout = ('{"type":"text","part":{"type":"text","text":'
              '"{\\"action\\":\\"approve\\",\\"lots\\":0.02}"}}\n')


def test_cli_advisor_retries_once_on_timeout(monkeypatch):
    import subprocess as sp
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sp.TimeoutExpired(cmd, kw.get("timeout", 0))
        return FakeProc()

    monkeypatch.setattr("xau_monitor.advisor.subprocess.run", fake_run)
    adv = CliAdvisor("m", 5.0, "SYS", binary="opencode",
                     cwd="/tmp", retries=1)
    d = adv.review(ctx())
    assert calls["n"] == 2
    assert d.action == "approve" and d.lots == pytest.approx(0.02)


def test_cli_advisor_fails_closed_after_all_retries(monkeypatch):
    import subprocess as sp
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr("xau_monitor.advisor.subprocess.run", fake_run)
    adv = CliAdvisor("m", 5.0, "SYS", binary="opencode",
                     cwd="/tmp", retries=1)
    with pytest.raises(AdvisorUnavailable) as exc:
        adv.review(ctx())
    assert calls["n"] == 2          # 1 percobaan + 1 retry
    assert "2x percobaan" in str(exc.value)


def test_cli_advisor_no_retry_when_disabled(monkeypatch):
    import subprocess as sp
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr("xau_monitor.advisor.subprocess.run", fake_run)
    adv = CliAdvisor("m", 5.0, "SYS", binary="opencode",
                     cwd="/tmp", retries=0)
    with pytest.raises(AdvisorUnavailable):
        adv.review(ctx())
    assert calls["n"] == 1
