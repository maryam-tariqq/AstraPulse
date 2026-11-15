import os

import pytest

import astra_backend as b

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "DB_PATH", str(tmp_path / "t.db"))
    b.init_db()


def put(sid, grp, ind, items, phase="inflight", posture=""):
    """items: {day: value}"""
    b.upsert([{"subject_id": sid, "grp": grp, "indicator": ind, "day": d, "phase": phase, "detail_phase": phase,
               "posture": posture, "value": v} for d, v in items.items()])


def chk(sid="X", grp="crew", day=20, phase="inflight", readings=None, **kw):
    return b.run_check(sid, grp, day, phase, readings, **kw)


def checks_of(out, ind):
    return {c["check"]: c for c in out["indicators"][ind]["checks"]}


# ---------------------------------------------------------------- rules file integrity
def test_every_indicator_and_alias_resolves():
    for g, v in b.GROUPS.items():
        for iid, ind in v["indicators"].items():
            assert b.resolve_indicator(g, iid) == iid
            for a in ind.get("aliases", []):
                assert b.resolve_indicator(g, a) == iid


def test_all_rule_sources_exist_in_bibliography():
    for g in b.GROUPS.values():
        refs = set()
        for ind in g["indicators"].values():
            for r in ind.get("cited_rules", []) + ind.get("window_rules", []):
                refs |= set(r.get("sources", []))
            for e in (ind.get("reference") or {}).values():
                refs |= set(e.get("sources", []))
        for c in g.get("compound_rules", []):
            refs |= set(c["sources"])
        assert refs <= set(b.SOURCES), refs - set(b.SOURCES)


def test_compound_indicators_exist_or_are_known_cross_group():
    known_cross = set(b.OWNER)      # compound rules may name indicators owned by another group (same person, same day)
    for gname, g in b.GROUPS.items():
        alias = b.COMPOUND_ALIAS.get(gname, {})
        def names(cl):
            for c in cl:
                if "any_of" in c:
                    yield from names(c["any_of"])
                elif "indicator" in c:
                    yield alias.get(c["indicator"], c["indicator"])
        for cr in g.get("compound_rules", []):
            for n in names(cr["requires"]):
                assert n in g["indicators"] or n.lower() in known_cross, (gname, cr["id"], n)


# ---------------------------------------------------------------- cited rules and boundaries
@pytest.mark.parametrize("v,status", [(5.9, "watch"), (6.0, "watch"), (6.01, "ok"), (7.0, "ok"), (8.0, "ok")])
def test_sleep_bands(v, status):
    out = chk(readings={"sleep_hours": v})
    assert out["indicators"]["sleep_hours"]["status"] == status


@pytest.mark.parametrize("v,status", [(2.29, "ok"), (2.3, "watch"), (2.99, "watch"), (3.0, "act"), (3.8, "act")])
def test_co2_bands_half_open(v, status):
    assert chk(grp="body_temperature", readings={"co2": v})["indicators"]["cabin_co2_mmHg"]["status"] == status


@pytest.mark.parametrize("v,status", [(78, "ok"), (77.9, "watch"), (70, "watch"), (69.9, "act")])
def test_vo2_inflight_bands(v, status):
    assert chk(readings={"vo2_pct": v})["indicators"]["vo2_pct_baseline"]["status"] == status


def test_phase_gated_rule_does_not_apply_in_other_phase():
    out = chk(phase="preflight", readings={"vo2_pct": 50})
    assert out["indicators"]["vo2_pct_baseline"]["status"] == "no_data"      # in-flight rules only


def test_rule_messages_carry_sources_and_audit_status():
    out = chk(grp="body_temperature", readings={"co2": 3.4})
    c = checks_of(out, "cabin_co2_mmHg")["cited:co2_ge_3"]
    assert c["nasa_cited"] and set(c["sources"]) == {"co2_ochmo", "co2_law_2016"}
    assert out["sources"]["co2_ochmo"]["url"].startswith("http") and out["sources"]["co2_ochmo"]["audit_status"]
    assert out["indicators"]["cabin_co2_mmHg"]["what_to_do"]


def test_uncited_defaults_are_labelled_and_switchable():
    on = chk(readings={"spo2": 89})["indicators"]["spo2_pct"]
    assert on["status"] == "act" and not on["checks"][0]["nasa_cited"] and "NOT NASA-cited" in on["checks"][0]["message"]
    off = chk(readings={"spo2": 89}, include_uncited=False)["indicators"]["spo2_pct"]
    assert off["status"] == "no_data"


# ---------------------------------------------------------------- window rules (7-day)
def test_window_rule_needs_min_days_then_fires():
    put("W", "crew", "sleep_hours", {17: 5.5, 18: 5.8, 19: 6.0})
    out = chk("W", day=20, readings={"sleep_hours": 6.0})           # 4 days present, need 5
    assert out["indicators"]["sleep_hours"]["not_scored"][0]["check"].startswith("window:")
    put("W", "crew", "sleep_hours", {16: 6.1})
    out = chk("W", day=20, readings={"sleep_hours": 5.9})           # 5 days, mean ~5.86 <= 6
    assert checks_of(out, "sleep_hours")["window:sleep_7day_mean_le_6"]["status"] == "act"


def test_window_ignores_days_older_than_seven():
    put("W", "crew", "sleep_hours", {1: 5.0, 2: 5.0, 3: 5.0, 4: 5.0, 5: 5.0})
    out = chk("W", day=20, readings={"sleep_hours": 7.5})
    assert out["indicators"]["sleep_hours"]["status"] == "ok"


# ---------------------------------------------------------------- personal 7-day same-phase baseline
def test_personal_baseline_flags_deviation_and_floors_sd():
    put("P", "crew", "resting_hr_bpm", {d: 60.0 for d in range(13, 20)})        # perfectly constant history
    ok = chk("P", day=20, readings={"hr_rest": 62})["indicators"]["resting_hr_bpm"]["personal_baseline"]
    assert ok["scored"] and ok["sd"] == 3.0 and ok["status"] == "ok"           # JSON baseline_min_sd = 3
    hi = chk("P", day=21, readings={"hr_rest": 70})["indicators"]["resting_hr_bpm"]
    assert hi["personal_baseline"]["status"] == "watch" and hi["status"] == "watch"
    assert hi["what_is_wrong"] and hi["what_to_do"]


def test_personal_baseline_is_same_phase_only():
    put("P", "crew", "resting_hr_bpm", {d: 60.0 for d in range(13, 20)}, phase="preflight")
    out = chk("P", day=20, phase="inflight", readings={"hr_rest": 75})["indicators"]["resting_hr_bpm"]
    pb = out["personal_baseline"]
    assert not pb["scored"] and pb["n"] == 0
    assert out["status"] == "no_data"                                            # unscored, not "ok"
    assert pb["cross_phase_info"]["z"] > 3                                       # shown for information only


def test_personal_baseline_needs_five_prior_days():
    put("P", "crew", "resting_hr_bpm", {16: 60.0, 17: 61.0, 18: 60.0, 19: 61.0})
    pb = chk("P", day=20, readings={"hr_rest": 90})["indicators"]["resting_hr_bpm"]["personal_baseline"]
    assert not pb["scored"] and pb["n"] == 4 and pb["needed"] == 5


def test_adverse_direction_respected():
    put("P", "crew", "resting_hr_bpm", {d: 60.0 for d in range(13, 20)})
    low = chk("P", day=20, readings={"hr_rest": 40})["indicators"]["resting_hr_bpm"]   # HR adverse only when high
    assert low["personal_baseline"]["status"] == "ok"
    put("P", "crew", "sleep_hours", {d: 7.5 for d in range(13, 20)})
    s = chk("P", day=20, readings={"sleep_hours": 7.2})["indicators"]["sleep_hours"]["personal_baseline"]
    assert s["status"] == "ok"
    put("P", "crew", "systolic_mmHg", {d: 110.0 for d in range(13, 20)})
    for v in (90, 130):                                                                   # BP adverse both ways
        assert chk("P", day=20, readings={"sbp": v}, save=False)["indicators"]["systolic_mmHg"]["personal_baseline"]["status"] == "watch"


def test_per_indicator_window_override_from_json():
    put("P", "crew", "pvt_lapses", {10: 1, 14: 2, 18: 1})                                 # sparse tests, 14-day window, min 3
    pb = chk("P", day=20, readings={"pvt_lapses": 9})["indicators"]["pvt_lapses"]["personal_baseline"]
    assert pb["scored"] and pb["window_days"] == 14 and pb["status"] == "watch"


# ---------------------------------------------------------------- NASA cohort reference (phase + posture)
def test_posture_required_for_edomp_reference():
    out = chk(phase="landing_day", readings={"hr_rest": 90})["indicators"]["resting_hr_bpm"]
    assert out["cohort_reference"]["applies"] is False and "posture" in out["cohort_reference"]["message"]


def test_landing_day_standing_sbp_is_flagged_but_capped_at_watch():
    out = chk(phase="landing_day", posture="standing", readings={"sbp": 70})["indicators"]["systolic_mmHg"]
    ref = out["cohort_reference"]
    assert ref["mean"] == 111 and ref["z"] < -3 and out["status"] == "watch"            # cohort value never gives "act"
    assert "presyncopal" in ref["landing_day_context"]
    assert out["what_to_do"]                                                            # JSON context_action


def test_reference_within_band_is_ok():
    out = chk(phase="preflight", posture="supine", readings={"hr_rest": 58})["indicators"]["resting_hr_bpm"]
    assert out["cohort_reference"]["status"] == "ok" and out["cohort_reference"]["sd_basis"] == "SD = SEM x sqrt(n)"


def test_postflight_reference_only_days_7_to_10():
    early = chk(phase="postflight", posture="supine", dsl=3, readings={"hr_rest": 80})["indicators"]["resting_hr_bpm"]
    assert early["cohort_reference"]["applies"] is False
    late = chk(phase="postflight", posture="supine", dsl=8, readings={"hr_rest": 80}, save=False)["indicators"]["resting_hr_bpm"]
    assert late["cohort_reference"]["applies"] and late["cohort_reference"]["status"] == "watch"


def test_provisional_reference_is_context_not_scored():
    out = chk(readings={"sleep_efficiency_pct": 60})["indicators"]["sleep_efficiency_pct"]
    assert out["cohort_reference"]["info_only"] and out["cohort_reference"]["z"] < -3
    assert out["status"] == "no_data"                                                   # nothing else scored it


# ---------------------------------------------------------------- compound predictions
def test_sleep_loss_plus_performance_fires_only_with_both():
    put("C", "crew", "pvt_lapses", {d: 2.0 for d in range(13, 20)})
    put("C", "crew", "sleep_hours", {d: 7.0 for d in range(13, 20)})
    bad = chk("C", day=20, readings={"sleep_hours": 5.0, "pvt_lapses": 12})
    assert [p["id"] for p in bad["predictions"]] == ["sleep_loss_plus_performance_decline"]
    assert bad["overall"]["status"] == "act" and bad["predictions"][0]["what_to_do"]
    fine = chk("C", day=21, readings={"sleep_hours": 7.5, "pvt_lapses": 12}, save=False)
    assert fine["predictions"] == []


def test_compound_uses_other_readings_entered_earlier_the_same_day():
    put("C", "crew", "pvt_lapses", {d: 2.0 for d in range(13, 20)})
    put("C", "crew", "sleep_hours", {d: 7.0 for d in range(13, 20)})
    chk("C", day=20, readings={"pvt_lapses": 12})
    out = chk("C", day=20, readings={"sleep_hours": 5.0})
    assert out["predictions"] and "pvt_lapses" in out["indicators"] and out["entered_indicators"] == ["sleep_hours"]


def test_compound_not_evaluated_without_baseline_is_reported_not_assumed_normal():
    out = chk("C2", readings={"sleep_hours": 5.0, "pvt_lapses": 12})
    assert out["predictions"] == []
    assert any(p["id"] == "sleep_loss_plus_performance_decline" for p in out["predictions_not_evaluated"])


def test_circadian_persistence_prediction():
    put("C", "crew", "circadian_misaligned", {16: 1, 17: 0, 18: 1, 19: 1})
    put("C", "crew", "sleep_hours", {d: 7.5 for d in range(13, 20)})
    out = chk("C", day=20, readings={"circadian_misaligned": True, "sleep_hours": 5.0})
    ids = [p["id"] for p in out["predictions"]]
    assert "persistent_sleep_loss_plus_circadian_misalignment" in ids
    assert checks_of(out, "circadian_misaligned")["window:circ_3_of_5"]["status"] == "act"


def test_orthostatic_prediction_after_landing():
    out = chk(phase="postflight", dsl=8, posture="standing", readings={"dizziness_standing_flag": True, "sbp": 78})
    assert any(p["id"] == "orthostatic_symptoms_plus_cardiovascular_change" for p in out["predictions"])
    assert out["overall"]["status"] == "act"
    none = chk("C3", phase="inflight", posture="standing", readings={"dizziness_standing_flag": True, "sbp": 78})
    assert not any(p["id"].startswith("orthostatic") for p in none["predictions"])        # landing/postflight only


def test_co2_symptom_compound_works_across_groups_in_either_order():
    """CO2 lives in body_temperature, headache in crew: the crew compound rule needs both, whichever is entered last."""
    put("C", "body_temperature", "cabin_co2_mmHg", {d: 2.7 for d in range(14, 20)})
    chk("C", "body_temperature", 20, readings={"cabin_co2_mmHg": 2.7})
    out = chk("C", day=20, readings={"headache_flag": True})                    # crew entered second
    assert any(p["id"] == "elevated_co2_plus_headache_or_sleep_disruption" for p in out["predictions"])
    put("D", "body_temperature", "cabin_co2_mmHg", {d: 2.7 for d in range(14, 20)})
    chk("D", day=20, readings={"headache_flag": True})                          # crew entered first: rule pending
    out = chk("D", "body_temperature", 20, readings={"cabin_co2_mmHg": 2.7})    # body_temperature second: merged in
    assert any(p["id"] == "elevated_co2_plus_headache_or_sleep_disruption" for p in out["predictions"])


# ---------------------------------------------------------------- missing data, groups, validation
def test_missing_today_is_listed():
    put("M", "crew", "sleep_hours", {19: 7.0})
    put("M", "crew", "spo2_pct", {19: 97.0})
    out = chk("M", day=20, readings={"sleep_hours": 7.0})
    assert out["missing_today"] == ["spo2_pct"]


def test_groups_are_never_pooled():
    put("G", "body_temperature", "cabin_co2_mmHg", {d: 2.0 for d in range(13, 20)})
    with pytest.raises(ValueError):
        chk("G", "crew", 20, readings={"cabin_co2_mmHg": 2.0})                          # CO2 is not a crew indicator
    with pytest.raises(ValueError):
        chk("G", "crew", 20, readings={"severity_0_3": 2})                              # MS indicator is not a crew indicator
    out = chk("G", "body_temperature", 20, readings={"cabin_co2_mmHg": 2.0}, save=False)
    assert abs(out["indicators"]["cabin_co2_mmHg"]["personal_baseline"]["mean"] - 2.0) < 1e-9


def test_input_validation():
    with pytest.raises(ValueError, match="plausible range"):
        chk(readings={"sleep_hours": 30})
    with pytest.raises(ValueError, match="unknown indicator"):
        chk(readings={"banana": 1})
    with pytest.raises(ValueError, match="unknown phase"):
        chk(phase="orbiting", readings={"sleep_hours": 7})
    with pytest.raises(ValueError, match="posture"):
        chk(readings={"sbp": 110}, posture="lying")
    with pytest.raises(ValueError):
        chk(readings={"sleep_hours": float("nan")})
    assert chk(readings={"circadian_misaligned": "yes"})["indicators"]["circadian_misaligned"]["value"] is True


def test_save_false_does_not_persist():
    chk("S", readings={"sleep_hours": 5.0}, save=False)
    assert b.list_subjects() == []
    chk("S", readings={"sleep_hours": 5.0})
    assert b.list_subjects() == ["S"]


# ---------------------------------------------------------------- motion sickness group
def test_motion_sickness_time_course_uses_flight_day():
    put("MS", "motion_sickness", "severity_0_3", {1: 0}, phase="preflight")
    put("MS", "motion_sickness", "severity_0_3", {2: 2, 3: 1}, phase="inflight")                # flight days 1-2
    early = chk("MS", "motion_sickness", 4, readings={"severity": 1}, save=False)["indicators"]["severity_0_3"]
    assert early["status"] == "ok"                                                              # flight day 3: mild, expected
    late = chk("MS", "motion_sickness", 5, readings={"severity": 1}, save=False)["indicators"]["severity_0_3"]
    assert late["status"] == "watch"                                                            # flight day 4: beyond ~72 h
    assert chk("MS", "motion_sickness", 5, readings={"severity": 2}, save=False)["indicators"]["severity_0_3"]["status"] == "act"
    assert chk("MS", "motion_sickness", 3, readings={"severity": 3}, save=False)["indicators"]["severity_0_3"]["status"] == "act"


def test_motion_sickness_person_context_from_profile():
    out = chk("MS2", "motion_sickness", 3, readings={"severity_0_3": 2},
              profile={"first_flight": True, "sex": "F", "age": 40})
    ids = [p["id"] for p in out["person_context"]]
    assert "first_flight" in ids and "no_predictors" in ids and "past_ms" not in ids
    assert out["indicators"]["severity_0_3"]["status"] in ("watch", "act")


# ---------------------------------------------------------------- body temperature group
def test_body_temperature_rules():
    assert chk("B", "body_temperature", 5, readings={"core_temp_exercise_peak_c": 40.2})["indicators"]["core_temp_exercise_peak_c"]["status"] == "watch"
    assert chk("B", "body_temperature", 5, readings={"core_temp_rest_c": 38.1}, save=False)["indicators"]["core_temp_rest_c"]["status"] == "watch"


def test_resting_core_temp_phase_shift_info():
    put("B", "body_temperature", "core_temp_rest_c", {1: 36.8, 2: 36.9, 3: 36.8}, phase="preflight")
    info = chk("B", "body_temperature", 30, readings={"core_temp_rest_c": 37.7})["indicators"]["core_temp_rest_c"]["phase_shift_info"]
    assert 0.8 < info["delta_vs_preflight"] < 1.0 and "expected direction" in info["message"]


def test_heat_strain_prediction_uses_crew_workload_across_groups():
    put("B", "body_temperature", "core_temp_exercise_peak_c", {d: 38.5 for d in range(13, 20)})
    put("B", "crew", "aerobic_min", {20: 45})                                   # today's aerobic workload (crew group)
    out = chk("B", "body_temperature", 20, readings={"core_temp_exercise_peak_c": 39.6})
    assert any(p["id"] == "exercise_heat_strain_pattern" for p in out["predictions"])


# ---------------------------------------------------------------- sample crew file (A01)
@pytest.fixture
def a01():
    return b.load_csv(os.path.join(HERE, "A01_30_days.csv"), reset=True)


def test_a01_load_mapping(a01):
    assert a01["subjects"] == ["A01"] and a01["observations"] == 600          # 30 days x 20 indicators
    assert a01["unmapped_columns"] == [] and a01["rejected"] == []
    S = b.load_subject("A01")
    assert len(S) == 20 and all(len(rows) == 30 for rows in S.values())
    assert ("body_temperature", "cabin_co2_mmHg") in S and ("motion_sickness", "severity_0_3") in S
    assert {r["phase"] for r in S[("crew", "sleep_hours")]} == {"preflight", "inflight", "postflight"}
    assert {r["posture"] for r in S[("crew", "sleep_hours")]} == {"supine", "standing"}      # posture kept from the CSV
    assert {r["posture"] for r in S[("body_temperature", "core_temp_rest_c")]} == {""}
    assert b.get_profile("A01") == {"sex": "F", "age": 45.0}


def test_a03_flight_day_6_first_scored_and_prediction():
    b.load_csv(os.path.join(HERE, "A03_30_days.csv"), reset=True)
    day12 = b.status_for_day("A03", 12, "crew")["groups"]["crew"]
    assert day12["flight_day"] == 7
    assert any(p["id"] == "sleep_loss_plus_performance_decline" for p in day12["predictions"])
    assert day12["indicators"]["resting_hr_bpm"]["personal_baseline"]["n"] >= 5
    early = b.status_for_day("A03", 8, "crew")["groups"]["crew"]               # only 3 inflight days so far
    assert not early["indicators"]["resting_hr_bpm"]["personal_baseline"]["scored"]


def test_csv_co2_headache_pattern_crosses_groups(a01):
    day2 = b.status_for_day("A01", 2, "crew")["groups"]["crew"]               # CO2 is in body_temperature, headache in crew
    assert any(p["id"] == "elevated_co2_plus_headache_or_sleep_disruption" for p in day2["predictions"])


def test_a01_new_entry_is_checked_against_a01_own_past_week_and_phase(a01):
    # in flight: previous 7 days (days 20-26) are all inflight -> full baseline from A01's own data
    out = b.run_check("A01", "crew", 27, "near_space_station_operations", {"resting_hr_bpm": 80},
                    posture="supine", save=False)
    hr = out["indicators"]["resting_hr_bpm"]["personal_baseline"]
    assert hr["scored"] and hr["phase"] == "inflight" and hr["window_days"] == 7 and hr["n"] == 7
    assert hr["status"] == "watch" and 61 < hr["mean"] < 66                      # A01 resting HR is about 63-65 in flight
    assert out["flight_day"] == 22
    # postflight: only 3 same-phase days exist, so it is NOT scored (and not called normal)
    post = b.run_check("A01", "crew", 31, "post_flight", {"sleep_hours": 4.8, "resting_hr_bpm": 80},
                       posture="standing", save=False)
    pb = post["indicators"]["resting_hr_bpm"]["personal_baseline"]
    assert not pb["scored"] and pb["n"] == 3 and post["indicators"]["resting_hr_bpm"]["status"] == "no_data"
    assert post["days_since_landing"] == 4 and post["notes"]                     # landing assumed from first postflight day
    assert post["indicators"]["sleep_hours"]["status"] == "watch"                # NASA rule still applies in any phase


def test_a01_replay_runs_and_separates_groups(a01):
    rows = b.timeline("A01")
    assert {r["group"] for r in rows} == {"crew", "motion_sickness", "body_temperature"}
    assert max(r["day"] for r in rows) == 30


# ---------------------------------------------------------------- API
def test_api_end_to_end(a01):
    pytest.importorskip("fastapi")           # needs pip install -r requirements.txt
    from fastapi.testclient import TestClient
    cl = TestClient(b.make_app())
    assert cl.get("/").json()["rules_version"].startswith("5")
    r = cl.post("/check", json={"subject_id": "A01", "group": "crew", "day": 31, "phase": "inflight",
                                "readings": {"sleep": 5.1, "headache": True}, "save": False})
    assert r.status_code == 200
    body = r.json()
    assert body["indicators"]["sleep_hours"]["status"] in ("watch", "act") and body["indicators"]["headache_flag"]["value"] is True
    assert body["sources"] and body["caveat"]
    r2 = cl.post("/check", json={"subject_id": "A01", "group": "body_temperature", "day": 31, "phase": "inflight",
                                 "readings": {"co2": 3.2}, "save": False})
    assert r2.status_code == 200 and r2.json()["indicators"]["cabin_co2_mmHg"]["status"] == "act"
    assert cl.post("/check", json={"subject_id": "A01", "day": 31, "phase": "inflight", "readings": {"sleep": 99}}).status_code == 422
    assert cl.post("/check", json={"subject_id": "A01", "group": "nope", "day": 31, "phase": "inflight", "readings": {"x": 1}}).status_code == 422
    assert cl.get("/subjects").json() == ["A01"]
    st = cl.get("/subjects/A01/status", params={"day": 2, "group": "crew"}).json()
    assert st["groups"]["crew"]["predictions"]
    assert cl.get("/subjects/A01/timeline").json()
    assert any(x["indicator"] == "resting_hr_bpm" for x in cl.get("/subjects/A01/baseline").json())
    assert cl.get("/subjects/NOPE/status").status_code == 404
    assert "sleep_hours" in cl.get("/indicators", params={"group": "crew"}).json()["crew"]
    assert cl.get("/indicators/crew/sleep").json()["label"] == "Sleep duration"
    assert "jones_2022_sleep" in cl.get("/sources").json()
