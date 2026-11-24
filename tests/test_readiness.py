"""Kesiapan trigger: persen gate, target harga per mode, dan format laporan."""
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor import readiness  # noqa: E402

ALL_GATES = ("seirama", "ekstrem", "veto", "zona", "sweep",
             "kocokan", "thrust", "trigger", "lastmin")


def diag(bias=None, ok=None, evaluated=None, zones=(), plan=None):
    gates = {key: {"ok": False} for key in ALL_GATES}
    for key, value in (ok or {}).items():
        gates[key]["ok"] = value
    return {"bias": bias, "gates": gates, "evaluated": list(evaluated or []),
            "zones": list(zones), "plan": plan, "missing": []}


def test_no_bias_gives_zero_and_no_target():
    rd = readiness.of_diag("sniper", diag(evaluated=["seirama"]), 3900.0)
    assert rd["percent"] == 0.0
    assert rd["remaining_pct"] == 100.0
    assert rd["gates_passed"] == 0
    assert rd["next_gate"] == "seirama"
    assert rd["trigger_price"] is None
    assert "bias" in rd["note"]


def test_partial_gate_progress_and_blocker():
    d = diag("SELL", ok={"seirama": True, "ekstrem": True, "zona": False},
             evaluated=["seirama", "ekstrem", "zona"])
    rd = readiness.of_diag("sniper", d, 3900.0)
    assert rd["percent"] == round(2 / 7 * 100, 1)
    assert rd["gates_passed"] == 2 and rd["gates_total"] == 7
    assert rd["next_gate"] == "zona"
    assert rd["next_label"] == "zona resis/sup"


def test_sell_target_is_nearest_zone_above_price():
    d = diag("SELL", ok={"seirama": True, "ekstrem": True, "zona": False},
             evaluated=["seirama", "ekstrem", "zona"],
             zones=[3895.0, 3904.0, 3918.0])
    rd = readiness.of_diag("sniper", d, 3900.0)
    assert rd["trigger_price"] == 3904.0
    assert rd["side"] == "SELL"
    assert rd["distance_usd"] == 4.0
    assert rd["distance_pct"] == pytest.approx(0.10, abs=1e-9)


def test_buy_target_is_nearest_zone_below_price():
    d = diag("BUY", ok={"seirama": True, "ekstrem": True}, evaluated=["seirama", "ekstrem"],
             zones=[3880.0, 3894.0])
    rd = readiness.of_diag("handgun", d, 3900.0)
    assert rd["trigger_price"] == 3894.0
    assert rd["distance_pct"] < 0            # harga harus turun ke support


def test_plan_zone_wins_over_candidate_list():
    plan = {"side": "SELL", "zone": 3891.5, "stop": 3893.8,
            "tps": [3881.0, 3877.0], "invalid": 3927.0}
    d = diag("SELL", ok={g: True for g in readiness.REQUIRED},
             evaluated=list(readiness.REQUIRED), zones=[3899.0], plan=plan)
    rd = readiness.of_diag("sniper", d, 3891.0)
    assert rd["trigger_price"] == 3891.5
    assert rd["plan"]["stop"] == 3893.8
    assert rd["percent"] == 100.0
    assert rd["next_gate"] is None
    assert "siap trigger" in rd["note"]


def test_lastmin_blocks_at_high_percent():
    ok = {g: True for g in readiness.REQUIRED}
    ok["lastmin"] = False
    d = diag("SELL", ok=ok, evaluated=list(readiness.REQUIRED), zones=[3895.0])
    rd = readiness.of_diag("sniper", d, 3900.0)
    assert rd["percent"] == round(6 / 7 * 100, 1)
    assert rd["next_gate"] == "lastmin"
    assert rd["next_label"] == "menit terakhir M15"


def test_modifier_gates_do_not_count():
    # sweep/thrust bukan syarat entry -> tidak menurunkan persen bila false
    ok = {g: True for g in readiness.REQUIRED}
    d = diag("SELL", ok=ok, evaluated=list(readiness.REQUIRED), zones=[3895.0])
    d["gates"]["sweep"]["ok"] = False
    d["gates"]["thrust"]["ok"] = False
    assert readiness.of_diag("sniper", d, 3900.0)["percent"] == 100.0


def test_fmt_line_shows_percent_target_and_next_gate():
    d = diag("SELL", ok={"seirama": True, "ekstrem": True, "zona": False},
             evaluated=["seirama", "ekstrem", "zona"], zones=[3904.0])
    line = readiness.fmt_line(readiness.of_diag("sniper", d, 3900.0))
    assert "SNIPER" in line
    assert "kesiapan 29%" in line            # 2/7
    assert "target <b>3904.00</b>" in line
    assert "+0.10%" in line and "naik" in line
    assert "zona resis/sup" in line


def test_fmt_line_without_target_is_explicit():
    line = readiness.fmt_line(readiness.of_diag("handgun", diag(evaluated=[]), 3900.0))
    assert "HANDGUN" in line and "belum ada level" in line


def test_extreme_gap_for_sell_bias():
    # SELL butuh harga NAIK TINGGI (>=80%) di TF entry — video 21:06.
    d = diag("SELL", ok={"seirama": True}, evaluated=["seirama"])
    d["extreme_tf"], d["extreme_pos"] = "M15", 62.5
    ex = readiness.of_diag("sniper", d, 3900.0)["extreme"]
    assert ex["need"] == 80.0 and ex["gap"] == 17.5 and ex["ok"] is False


def test_extreme_without_bias_shows_both_thresholds():
    d = diag(evaluated=["seirama"])
    d["extreme_tf"], d["extreme_pos"] = "M15", 55.0
    ex = readiness.of_diag("handgun", d, 3900.0)["extreme"]
    assert ex["need"] is None and ex["gap"] is None and ex["ok"] is None


def test_extreme_gap_zero_when_already_extreme():
    # SELL di M15 pos20 91% = sudah naik tinggi -> gate ekstrem lolos.
    d = diag("SELL", ok={"seirama": True, "ekstrem": True},
             evaluated=["seirama", "ekstrem"])
    d["extreme_tf"], d["extreme_pos"] = "M15", 91.0
    ex = readiness.of_diag("handgun", d, 3900.0)["extreme"]
    assert ex["ok"] is True and ex["gap"] == 0.0


def test_watch_levels_shown_when_no_bias():
    d = diag(evaluated=["seirama"])
    d["watch"] = {"res_above": [3872.4], "sup_below": [3858.1]}
    rd = readiness.of_diag("sniper", d, 3865.0)
    assert "res 3872.40" in rd["note"] and "sup 3858.10" in rd["note"]
