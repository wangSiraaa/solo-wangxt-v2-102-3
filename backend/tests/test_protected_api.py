
def _carrier(name, center=100.0, power=-13.0103, pol="H"):
    return {"name": name, "center_mhz": center, "bandwidth_mhz": 4.0,
            "power_dbm": power, "polarization": pol, "mask_name": "strict"}


def test_protected_receiver_aggregate_endpoint_lists_contributions(client):
    create = client.post("/api/protected-receivers", json={
        "receiver_key": "rx-agg", "name": "聚合保护窗",
        "window_low_mhz": 93, "window_high_mhz": 113,
        "power_limit_dbm": -11, "polarizations": ["H"]})
    assert create.status_code == 200
    body = {"carriers": [_carrier("A", 100), _carrier("B", 106)],
            "rules": {"guard_required_mhz": 1, "leakage_limit_dbm": -45},
            "protected_receiver_keys": ["rx-agg"]}
    res = client.post("/api/analyze", json=body).json()
    assert res["protected_receiver_checks"][0]["status"] == "total_exceeded"
    assert res["rule_binding"]["protected_receivers"][0]["version"] == 1
    assert res["rule_freshness"]["stale"] is False
    assert {c["carrier"] for c in res["protected_receiver_checks"][0]["contributions"]} == {"A", "B"}


def test_single_carrier_contribution_violation_is_reported(client):
    client.post("/api/protected-receivers", json={
        "receiver_key": "rx-single", "name": "单载波保护窗",
        "window_low_mhz": 98, "window_high_mhz": 102,
        "power_limit_dbm": -20, "polarizations": ["H"]})
    body = {"carriers": [_carrier("A", 100, power=-13.0103, pol="H")],
            "rules": {"guard_required_mhz": 1, "leakage_limit_dbm": -45},
            "protected_receiver_keys": ["rx-single"]}
    res = client.post("/api/analyze", json=body).json()
    types = [f["type"] for f in res["findings"]]
    assert "protected_receiver_contribution" in types
    assert "protected_receiver_total" in types
    assert res["protected_receiver_checks"][0]["status"] == "total_exceeded"


def test_unknown_polarization_endpoint_is_pending(client):
    client.post("/api/protected-receivers", json={
        "receiver_key": "rx-unknown", "name": "未知隔离保护窗",
        "window_low_mhz": 93, "window_high_mhz": 113,
        "power_limit_dbm": -9, "polarizations": ["H", "V"]})
    body = {"carriers": [_carrier("H1", 100, pol="H"), _carrier("V1", 106, pol="V")],
            "rules": {"guard_required_mhz": 1, "leakage_limit_dbm": -45,
                      "reuse_policy": {"H|V": "unknown"}},
            "protected_receiver_keys": ["rx-unknown"]}
    res = client.post("/api/analyze", json=body).json()
    check = res["protected_receiver_checks"][0]
    assert check["status"] == "polarization_unknown"
    assert check["unknown_polarization_pairs"] == ["H1|V1"]
    assert res["counts"]["error"] == 0
    assert res["counts"]["pending"] == 1


def test_receiver_revision_marks_old_report_stale_but_reproducible(client):
    assert client.post("/api/protected-receivers", json={
        "receiver_key": "rx-version", "name": "版本保护窗",
        "window_low_mhz": 93, "window_high_mhz": 113,
        "power_limit_dbm": -11, "polarizations": ["H"]}).status_code == 200
    body = {"carriers": [_carrier("A", 100), _carrier("B", 106)],
            "rules": {"guard_required_mhz": 1, "leakage_limit_dbm": -45},
            "protected_receiver_keys": ["rx-version"],
            "scenario_id": None}
    first = client.post("/api/analyze", json=body).json()
    report_id = first["id"]
    old_total = first["protected_receiver_checks"][0]["total_power_dbm"]
    assert first["protected_receiver_checks"][0]["power_limit_dbm"] == -11

    revised = client.post("/api/protected-receivers/rx-version/revisions", json={
        "name": "版本保护窗-收紧", "window_low_mhz": 95, "window_high_mhz": 115,
        "power_limit_dbm": -100, "polarizations": ["H"]})
    assert revised.status_code == 200
    assert revised.json()["version"] == 2

    stored = client.get(f"/api/analysis/{report_id}").json()
    assert stored["rule_binding"]["protected_receivers"][0]["version"] == 1
    assert stored["protected_receiver_checks"][0]["power_limit_dbm"] == -11
    assert stored["protected_receiver_checks"][0]["total_power_dbm"] == old_total
    assert stored["rule_freshness"]["stale"] is True
    assert stored["rule_freshness"]["protected_receivers"][0]["state"] == "superseded"

    fresh = client.post("/api/analyze", json=body).json()
    assert fresh["rule_binding"]["protected_receivers"][0]["version"] == 2
    assert fresh["rule_freshness"]["stale"] is False
    assert fresh["protected_receiver_checks"][0]["power_limit_dbm"] == -100


def test_plan_post_check_uses_protected_window(client):
    client.post("/api/protected-receivers", json={
        "receiver_key": "rx-plan", "name": "规划保护窗",
        "window_low_mhz": 100, "window_high_mhz": 110,
        "power_limit_dbm": -20, "polarizations": ["H", "V"]})
    body = {"carriers": [_carrier("H1", 100, pol="H"), _carrier("V1", 100, pol="V")],
            "rules": {"guard_required_mhz": 1, "leakage_limit_dbm": -45,
                      "reuse_policy": {"H|V": "unknown"}},
            "protected_receiver_keys": ["rx-plan"],
            "band_low_mhz": 80, "band_high_mhz": 130, "mode": "guard_only"}
    res = client.post("/api/plan", json=body).json()
    assert res["feasible"]
    assert res["post_check"]["counts"]["error"] == 0
    assert res["post_check"]["counts"]["pending"] == 0
    assert res["post_check"]["protected_receiver_checks"][0]["status"] == "ok"

    body["band_low_mhz"] = 95
    body["band_high_mhz"] = 115
    tight = client.post("/api/plan", json=body).json()
    assert tight["feasible"] is False
