"""受保护接收机：线性总量、未知极化隔离与规划约束测试。"""
from app.services.analysis import (AnalysisRules, Carrier, ProtectedReceiver,
                                   analyze)
from app.services.planner import BandLimits, plan


def carrier(name, center, power=-13.0103, pol="H"):
    return Carrier(id=None, name=name, center_mhz=center, bandwidth_mhz=4.0,
                   power_dbm=power, polarization=pol, mask_name="strict")


def rx(limit=-11.0, pols=("H",), version=1):
    return ProtectedReceiver(
        receiver_key="rx-test", version=version, name="测试保护接收机",
        window_low_mhz=93.0, window_high_mhz=113.0,
        power_limit_dbm=limit, polarizations=pols)


def rules(rec, policy=None):
    return AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                         reuse_policy=policy or {}, protected_receivers=[rec])


def test_two_individually_ok_carriers_exceed_linear_total():
    res = analyze([carrier("A", 100.0), carrier("B", 106.0)], rules(rx(-11.0)))
    check = res["protected_receiver_checks"][0]
    assert check["status"] == "total_exceeded"
    assert check["contributions"][0]["leakage_dbm"] < -11.0
    assert check["contributions"][1]["leakage_dbm"] < -11.0
    assert abs(check["total_power_dbm"] + 10.0) < 0.25
    totals = [f for f in res["findings"] if f["type"] == "protected_receiver_total"]
    assert totals and totals[0]["severity"] == "error"
    assert {c["carrier"] for c in totals[0]["contributions"]} == {"A", "B"}


def test_unknown_polarization_isolation_is_pending_not_pass_or_violation():
    rec = rx(limit=-9.0, pols=("H", "V"))
    res = analyze([carrier("H1", 100.0, pol="H"), carrier("V1", 106.0, pol="V")],
                  rules(rec, {"H|V": "unknown"}))
    check = res["protected_receiver_checks"][0]
    assert check["status"] == "polarization_unknown"
    assert check["unknown_polarization_pairs"] == ["H1|V1"]
    totals = [f for f in res["findings"] if f["type"] == "protected_receiver_total"]
    assert totals and totals[0]["severity"] == "pending"
    assert res["counts"]["error"] == 0
    assert res["counts"]["pending"] == 1


def test_planner_avoids_protected_window_and_infeasible_when_tight():
    rec = ProtectedReceiver("rx-plan", 1, "规划保护窗", 100.0, 110.0,
                            -20.0, ("H", "V"))
    data = [carrier("H1", 100.0, pol="H"), carrier("V1", 100.0, pol="V")]
    r = plan(data, rules(rec, {"H|V": "unknown"}), BandLimits(80.0, 130.0))
    assert r["feasible"]
    post = r["post_check"] if "post_check" in r else None
    # plan() 自身不附加 post_check；这里用相同规则显式复核。
    if post is None:
        planned = [Carrier(None, a["name"], a["center_mhz"], a["bandwidth_mhz"],
                           a["power_dbm"], a["polarization"], a["mask_name"])
                   for a in r["assignments"]]
        post = analyze(planned, rules(rec, {"H|V": "unknown"}))
    assert post["counts"]["error"] == 0
    assert post["counts"]["pending"] == 0
    check = post["protected_receiver_checks"][0]
    assert check["status"] == "ok"
    assert check["total_power_dbm"] is None

    tight = plan(data, rules(rec, {"H|V": "unknown"}), BandLimits(95.0, 115.0))
    assert not tight["feasible"]
