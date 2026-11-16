"""
astra_backend.py : ASTRA PULSE health-check backend (FastAPI + SQLite, single file), driven by astra_rules.json (v5).

Run:
    pip install -r requirements.txt
    python astra_backend.py load A01_30_days.csv --reset        # load the sample crew data
    python astra_backend.py replay A01                          # day-by-day verdicts for that crew member
    python astra_backend.py check --subject A01 --day 31 --phase postflight --set sleep_hours=5.2 --group body_temperature --set cabin_co2_mmHg=2.9
    python astra_backend.py serve                               # API + docs at http://127.0.0.1:8000/docs

What one check does (per indicator the person enters)
  1. NASA-cited absolute rules from the JSON (cited_rules, window_rules, label_passthrough), gated by phase.
  2. NASA cohort reference (EDOMP etc.) for the current phase and posture, when the JSON has one. Cohort values are
     descriptive, so they can raise "watch" at most, never "act" (JSON reference_audit: not a universal limit).
  3. The person's OWN previous 7 days (min 5 days present) in the SAME flight phase and posture. This is an app-level
     comparison, not a NASA limit. Indicators with a different window (pvt_lapses, poms_score, il1ra) use the JSON's.
  4. Compound predictions from the JSON (e.g. short sleep + slower reactions), using that day's other readings.
Missing data is never treated as normal: unscored checks say why.

Groups are separate populations (crew, motion_sickness, body_temperature): readings, baselines and references are
never pooled across groups. Decision-support prototype, not a medical device.
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.environ.get("ASTRA_RULES", os.path.join(HERE, "astra_rules.json"))
DB_PATH = os.environ.get("ASTRA_DB", "astra.db")

SEV = {"ok": 0, "watch": 1, "act": 2}
NAMES = ["ok", "watch", "act"]
Z_REFERENCE = 1.96          # central 95% of a normal distribution, applied to NASA cohort mean +/- SD (convention)
Z_PERSONAL = 2.0            # own-baseline deviation that raises "watch" (team choice)
PERSONAL_WINDOW_DAYS = 7    # requested: compare with the person's own past 5 to 7 days
PERSONAL_MIN_DAYS = 3
GENERIC_ACTION = ("Re-measure under the same conditions, check sleep, exercise, hydration and cabin conditions, and "
                  "report to the flight surgeon if it persists or you feel unwell. (App default, not a NASA rule.)")
CAVEAT = ("Decision support only, not diagnosis or treatment. Escalate concerning symptoms to the flight surgeon. "
          "Personal-baseline checks are app-level logic, not NASA limits.")

PHASE_MAP = {
    "preflight": "preflight", "pre_flight": "preflight", "pre-flight": "preflight", "pre": "preflight",
    "training": "preflight", "baseline": "preflight",
    "inflight": "inflight", "in_flight": "inflight", "in-flight": "inflight", "flight": "inflight",
    "orbit": "inflight", "transit": "inflight", "launch": "inflight", "launch_adaptation": "inflight",
    "in_flight_cruise": "inflight", "near_space_station_operations": "inflight", "iss": "inflight",
    "iss_early": "inflight", "iss_mid": "inflight", "iss_late": "inflight", "lunar_surface": "inflight",
    "landing_day": "landing_day", "landing": "landing_day", "reentry": "landing_day", "re-entry": "landing_day",
    "postflight": "postflight", "post_flight": "postflight", "post-flight": "postflight", "post": "postflight",
    "recovery": "postflight",
}

# ------------------------------------------------------------------ CSV mapping
# The 20-indicator crew CSVs use the rules-file indicator ids as column names, so columns are mapped automatically
# (see build_csv_map after the rules are loaded). Only these columns are metadata, not indicators.
CSV_META = ("astronaut_id", "sex", "age", "mission_day", "mission_phase", "posture")
CSV_ASSUMPTIONS = [
    "Every CSV column whose name is an indicator id (or alias) in astra_rules.json is loaded into that indicator's group: "
    "crew, motion_sickness (severity_0_3) or body_temperature (core_temp_*, cabin_co2_mmHg).",
    "The 'posture' column (supine/standing) is stored with the crew readings so posture-specific NASA references and "
    "own-baseline comparisons (same phase + same posture) work; the other groups carry no posture.",
    "Boolean columns (circadian_misaligned, headache_flag, dizziness_standing_flag) are stored as 0/1.",
    "severity_0_3 is the CSV's overall symptom severity and is loaded into the motion_sickness group.",
]
CSV_PROFILE = {"sex": "sex", "age": "age"}


# ------------------------------------------------------------------ rules file
def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    if not str(r.get("meta", {}).get("version", "")).startswith("5"):
        raise SystemExit(f"{path}: expected rules version 5.x, found {r.get('meta', {}).get('version')}")
    for gname, g in r["groups"].items():
        seen = {}
        for iid, ind in g["indicators"].items():
            for a in [iid] + ind.get("aliases", []):
                if seen.setdefault(a.lower(), iid) != iid:
                    raise SystemExit(f"alias clash in {gname}: {a}")
    return r


RULES = load_rules()
GROUPS = RULES["groups"]
SOURCES = RULES["sources"]
# Compound rules may name an indicator that lives in another group (the crew CO2 rule needs cabin_co2_mmHg from
# body_temperature; the heat-strain rule needs resting_hr_bpm / aerobic_min from crew). OWNER finds that group.
# Same person + same day only: baselines and references are still never pooled across groups.
COMPOUND_ALIAS = {}
OWNER = {}
for _g, _gd in GROUPS.items():
    for _iid, _ind in _gd["indicators"].items():
        for _n in [_iid] + _ind.get("aliases", []):
            OWNER.setdefault(_n.lower(), (_g, _iid))
CSV_MAP = {n: gi for n, gi in OWNER.items()}     # lower-cased column name -> (group, indicator)


def resolve_indicator(group, name):
    if group not in GROUPS:
        raise ValueError(f"unknown group '{group}', use one of {list(GROUPS)}")
    low = str(name).lower()
    for iid, ind in GROUPS[group]["indicators"].items():
        if low == iid.lower() or low in [a.lower() for a in ind.get("aliases", [])]:
            return iid
    raise ValueError(f"unknown indicator '{name}' in group '{group}'")


def norm_phase(p):
    key = str(p).strip().lower()
    if key not in PHASE_MAP:
        raise ValueError(f"unknown phase '{p}'. Use preflight, inflight, landing_day or postflight "
                         f"(also accepted: {', '.join(sorted(k for k in PHASE_MAP if k not in ('preflight','inflight','landing_day','postflight')))})")
    return PHASE_MAP[key]


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    raise ValueError(f"cannot read '{v}' as yes/no")


def clean_value(group, iid, raw):
    """Validate one entered value against the JSON type and sanity_range. Returns a float (booleans as 0/1)."""
    ind = GROUPS[group]["indicators"][iid]
    if ind["type"] == "bool":
        return 1.0 if to_bool(raw) else 0.0
    if isinstance(raw, bool):
        raise ValueError(f"{iid}: expected a number")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{iid}: '{raw}' is not a number")
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"{iid}: not a finite number")
    lo, hi = ind.get("sanity_range") or (None, None)
    if lo is not None and not (lo <= v <= hi):
        raise ValueError(f"{iid}: {v:g} is outside the plausible range {lo:g}-{hi:g} {ind['unit']} (check units)")
    return v


# ------------------------------------------------------------------ storage
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS observations (subject_id TEXT, grp TEXT, indicator TEXT, day INTEGER, "
                  "phase TEXT, detail_phase TEXT, posture TEXT, value REAL, hour REAL, recorded_at TEXT, "
                  "PRIMARY KEY (subject_id, grp, indicator, day, posture))")
        c.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id TEXT PRIMARY KEY, profile TEXT)")


def upsert(rows):
    """rows: dicts with subject_id, grp, indicator, day, phase, detail_phase, posture, value, hour."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                      [(r["subject_id"], r["grp"], r["indicator"], r["day"], r["phase"], r.get("detail_phase") or r["phase"],
                        r.get("posture") or "", r["value"], r.get("hour"), now) for r in rows])


def load_subject(sid):
    """All observations of one subject: {(group, indicator): [rows sorted by day]}."""
    init_db()
    S: Dict[Any, List[dict]] = {}
    with conn() as c:
        for r in c.execute("SELECT * FROM observations WHERE subject_id=? ORDER BY day", (sid,)):
            S.setdefault((r["grp"], r["indicator"]), []).append(dict(r))
    return S


def get_profile(sid):
    init_db()
    with conn() as c:
        r = c.execute("SELECT profile FROM subjects WHERE subject_id=?", (sid,)).fetchone()
    return json.loads(r[0]) if r else {}


def set_profile(sid, profile):
    merged = {**get_profile(sid), **{k: v for k, v in (profile or {}).items() if v is not None}}
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO subjects VALUES (?,?)", (sid, json.dumps(merged)))
    return merged


def list_subjects():
    init_db()
    with conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT subject_id FROM observations ORDER BY subject_id")]


def overlay(S, rows):
    """Return S with extra rows merged in memory (used so a check can run before/without saving)."""
    out = {k: list(v) for k, v in S.items()}
    for r in rows:
        key = (r["grp"], r["indicator"])
        lst = [x for x in out.get(key, []) if not (x["day"] == r["day"] and x["posture"] == (r.get("posture") or ""))]
        lst.append({**r, "posture": r.get("posture") or ""})
        out[key] = sorted(lst, key=lambda x: x["day"])
    return out


# ------------------------------------------------------------------ context helpers
@dataclass
class Ctx:
    subject_id: str
    day: int
    phase: str                       # preflight | inflight | landing_day | postflight
    detail_phase: str
    posture: str = ""
    dsl: Optional[int] = None        # days since landing
    flight_day: Optional[int] = None
    profile: dict = field(default_factory=dict)
    include_uncited: bool = True
    notes: List[str] = field(default_factory=list)


def landing_info(S):
    """(landing_day, assumed?) from stored data: a landing_day row, else one day before the first postflight row."""
    days = sorted({r["day"] for rows in S.values() for r in rows if r["phase"] == "landing_day"})
    if days:
        return days[0], False
    post = sorted({r["day"] for rows in S.values() for r in rows if r["phase"] == "postflight"})
    return ((post[0] - 1, True) if post else (None, False))


def first_inflight_day(S):
    d = [r["day"] for rows in S.values() for r in rows if r["phase"] == "inflight"]
    return min(d) if d else None


def build_ctx(sid, S, day, phase, detail, posture="", dsl=None, profile=None, include_uncited=True):
    ctx = Ctx(sid, day, phase, detail, posture or "", dsl, None, profile or {}, include_uncited)
    if ctx.dsl is None and phase in ("landing_day", "postflight"):
        ld, assumed = landing_info(S)
        if ld is not None:
            ctx.dsl = max(day - ld, 0)
            if assumed:
                ctx.notes.append(f"No landing-day row stored: landing assumed on day {ld} (day before first postflight day).")
    fid = first_inflight_day(S)
    if phase == "inflight" and fid is not None:
        ctx.flight_day = day - fid + 1
    return ctx


def mission_day_for_rules(ctx):
    """NASA time courses count from launch, so use flight day when known, else the entered day."""
    return ctx.flight_day if ctx.flight_day is not None else ctx.day


def when_ok(when, ctx):
    if not when:
        return True
    if "phase" in when and ctx.phase not in when["phase"]:
        return False
    if "mission_day_min" in when and mission_day_for_rules(ctx) < when["mission_day_min"]:
        return False
    if "days_since_landing_min" in when and (ctx.dsl is None or ctx.dsl < when["days_since_landing_min"]):
        return False
    return True


def op_test(op, v, target):
    if op == "le":
        return v <= target
    if op == "lt":
        return v < target
    if op == "ge":
        return v >= target
    if op == "gt":
        return v > target
    if op == "eq":
        if isinstance(target, bool):
            return bool(v) == target
        return abs(v - float(target)) < 1e-9
    if op == "between":                      # half-open [lo, hi): adjacent bands in the JSON then never overlap
        return target[0] <= v < target[1]
    raise ValueError(f"unknown op {op}")


# ------------------------------------------------------------------ sources
def src(sid):
    s = SOURCES.get(sid, {})
    return {"id": sid, "citation": s.get("citation", sid), "url": s.get("verified_primary_url") or s.get("url"),
            "nasa_link": s.get("nasa_link"), "verified": s.get("verified"),
            "audit_status": s.get("bibliography_audit_status"),
            "audit_note": s.get("bibliography_audit_note"),
            "caution": ("bibliography only partially verified" if s.get("bibliography_audit_status") in
                        ("partially_verified", "unverified_placeholder") else None)}


def chk(check, status, message, action=None, sources=None, nasa_cited=True, evidence=None, **extra):
    d = {"check": check, "status": status, "message": message, "action": action, "sources": sources or [],
         "nasa_cited": nasa_cited, "evidence": evidence}
    d.update(extra)
    return d


# ------------------------------------------------------------------ individual checks
def series(S, grp, iid, lo, hi, phase=None, posture=None):
    return [r for r in S.get((grp, iid), []) if lo <= r["day"] <= hi
            and (phase is None or r["phase"] == phase) and (posture is None or r["posture"] == posture)]


def slope(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else 0.0


def check_cited(ind, v, ctx):
    hits = [r for r in ind.get("cited_rules", []) if when_ok(r.get("when"), ctx) and op_test(r["test"]["op"], v, r["test"]["value"])]
    if not hits:
        return []
    worst = max(SEV[r["status"]] for r in hits)
    return [chk(f"cited:{r['id']}", r["status"], r["message"], r.get("action"), r.get("sources"), True,
                r.get("evidence_level")) for r in hits if SEV[r["status"]] == worst]


def check_windows(S, grp, iid, ind, ctx):
    out, unscored = [], []
    for w in ind.get("window_rules", []):
        rows = series(S, grp, iid, ctx.day - w["days"] + 1, ctx.day)
        by_day = {}
        for r in rows:
            by_day[r["day"]] = r["value"]
        vals = list(by_day.values())
        if len(vals) < w["min_days"]:
            unscored.append({"check": f"window:{w['id']}", "status": "no_data",
                             "message": f"{len(vals)} of the last {w['days']} days present, need {w['min_days']}."})
            continue
        stat = statistics.fmean(vals) if w["stat"] == "mean" else sum(1 for x in vals if x)
        fired = op_test(w["op"], stat, w["value"])
        msg = w["message"] if fired else f"{w['id']}: {w['stat']} over last {len(vals)} days = {stat:.2f}, rule not met."
        out.append(chk(f"window:{w['id']}", w["status"] if fired else "ok", msg, w.get("action") if fired else None,
                       w.get("sources"), True, w.get("evidence_level"), window_value=round(stat, 3), days_present=len(vals)))
    return out, unscored


def check_labels(ind, v):
    lp = ind.get("label_passthrough")
    if not lp:
        return []
    key = "true" if (ind["type"] == "bool" and v) else str(int(v)) if ind["type"] != "bool" else None
    st = lp.get(key) if key else None
    if not st:
        return []
    note = ind.get("no_nasa_reference", "")
    return [chk("label", st, f"{ind['label']} is set ({'yes' if ind['type'] == 'bool' else int(v)}). {note}",
                "Review with the flight surgeon under mission protocol." if st == "act" else None,
                [], False, "user_defined_label")]


def check_uncited(ind, v, ctx):
    out = []
    if not ctx.include_uncited:
        return out
    hits = [d for d in ind.get("uncited_defaults", []) if op_test(d["op"], v, d["value"])]
    if hits:
        worst = max(SEV[d["status"]] for d in hits)
        for d in hits:
            if SEV[d["status"]] == worst:
                out.append(chk(f"generic:{d['id']}", d["status"], f"{d['message']} (Basis: {d['basis']}.)",
                               "Re-measure; report to the flight surgeon if confirmed or symptomatic.", [], False,
                               "generic_clinical_convention"))
    return out


def ref_key(ctx):
    if ctx.phase == "postflight":
        return "postflight_d7_10" if ctx.dsl is not None and 7 <= ctx.dsl <= 10 else None
    return ctx.phase


def check_reference(ind, v, ctx):
    refs = ind.get("reference")
    if not refs:
        return None
    pk = ref_key(ctx)
    note = None
    if pk is None:
        return {"applies": False, "message": "No NASA numeric reference for this phase" + (
            " (EDOMP measured postflight days 7-10 only; days 1-6 have no numeric reference)." if ctx.phase == "postflight" else ".")}
    entry = refs.get(f"{pk}|{ctx.posture}") if ctx.posture else None
    if entry is None:
        entry = refs.get(f"{pk}|any")
    if entry is None:
        postures = sorted(k.split("|")[1] for k in refs if k.startswith(pk + "|"))
        if not postures:
            return {"applies": False, "message": "No NASA numeric reference for this phase."}
        return {"applies": False, "message": f"NASA reference exists for this phase but is posture-specific: send posture ({', '.join(postures)}) to compare."}
    sd = entry.get("sd")
    if sd is None and entry.get("sem") is not None and entry.get("n"):
        sd = entry["sem"] * math.sqrt(entry["n"])
    basis = "SD given in the JSON" if entry.get("sd") is not None else "SD = SEM x sqrt(n)"
    bad = entry.get("bad") or ind.get("baseline_bad")
    out = {"applies": True, "label": entry["label"], "mean": entry["mean"], "sd": round(sd, 2) if sd else None,
           "sd_basis": basis if sd else None, "sources": entry.get("sources", []), "z": None, "status": "ok"}
    info_only = bool(entry.get("info_only")) or "provisional" in entry["label"].lower() or \
        str(ind.get("reference_audit_status", "")).startswith("provisional")
    if not sd:
        out.update(message=f"{entry['label']}: mean {entry['mean']}. No spread available, so shown for context only.", info_only=True)
        return out
    z = (v - entry["mean"]) / sd
    out["z"] = round(z, 2)
    adverse = (z >= Z_REFERENCE if bad == "high" else z <= -Z_REFERENCE if bad == "low" else abs(z) >= Z_REFERENCE)
    out["info_only"] = info_only
    where = "above" if z > 0 else "below"
    if info_only:
        out["message"] = (f"{v:g} is {abs(z):.1f} SD {where} the NASA example {entry['label']} (mean {entry['mean']}, SD {sd:.1f}). "
                          "Descriptive/provisional reference: shown for context, not scored.")
    elif adverse:
        out["status"] = "watch"
        out["message"] = (f"{v:g} is {abs(z):.1f} SD {where} the NASA cohort mean for {entry['label']} (mean {entry['mean']}, SD {sd:.1f}; "
                          f"outside the central 95% band). Cohort values are descriptive, not individual limits.")
    else:
        out["message"] = (f"{v:g} is within the NASA cohort band for {entry['label']} (mean {entry['mean']}, SD {sd:.1f}, z {z:+.1f}).")
    # presyncopal vs non-presyncopal landing-day context (EDOMP Table 1-6): informational only
    cg = (ind.get("context_groups") or {}).get(f"{pk}|{ctx.posture}") if ctx.posture else None
    if cg and len(cg) == 2:
        a, b = cg[0], cg[1]
        near = a if abs(v - a["mean"]) < abs(v - b["mean"]) else b
        out["landing_day_context"] = (f"Landing-day stand test groups: {a['label']} mean {a['mean']}, {b['label']} mean {b['mean']}. "
                                      f"Your value is nearer the {near['label']} group. Informational only, not a diagnosis.")
    return out


def check_delta_phase(S, grp, iid, ind, v, ctx):
    d = ind.get("delta_from_phase")
    if not d or ctx.phase not in d["applies_to_phase"]:
        return None
    base = [r["value"] for r in S.get((grp, iid), []) if r["phase"] == d["baseline_phase"]]
    if len(base) < d["min_days"]:
        return {"message": f"Needs {d['min_days']} {d['baseline_phase']} readings to compare with your own {d['baseline_phase']} mean; have {len(base)}."}
    delta = v - statistics.fmean(base)
    floor = ind.get("baseline_min_sd", 0.0)
    kind = "above" if delta > d["expected_delta"] else "none" if delta < floor else "typical"
    return {"delta_vs_preflight": round(delta, 2), "expected_delta_iss_mean": d["expected_delta"], "sources": d["sources"],
            "message": d["text"][kind].format(delta=delta),
            "note": f"Information only. 'none' means below {floor:g} C (the indicator's noise floor, an app choice)."}


def check_personal(S, grp, iid, ind, v, ctx):
    bad = ind.get("baseline_bad")
    if ind["type"] != "number" or bad not in ("low", "high", "both"):
        return None
    W = ind.get("baseline_window_days", PERSONAL_WINDOW_DAYS)
    need = ind.get("baseline_min_days", PERSONAL_MIN_DAYS)
    prior = [r for r in series(S, grp, iid, ctx.day - W, ctx.day - 1, ctx.phase, ctx.posture)]
    vals = [r["value"] for r in prior]
    out = {"scored": False, "window_days": W, "phase": ctx.phase, "posture": ctx.posture or None, "n": len(vals), "needed": need,
           "basis": f"your own previous {W} days in phase '{ctx.phase}'"}
    if len(vals) < need:
        out["reason"] = (f"Only {len(vals)} previous {ctx.phase} reading(s) in the last {W} days; need {need} to build your baseline. "
                         "Not scored (missing baseline is not treated as normal).")
        other = [r["value"] for r in series(S, grp, iid, ctx.day - W, ctx.day - 1, None, ctx.posture) if r["phase"] != ctx.phase]
        if len(other) >= need:
            m, sd = statistics.fmean(other), max(statistics.stdev(other), ind.get("baseline_min_sd", 0))
            out["cross_phase_info"] = {"mean": round(m, 2), "sd": round(sd, 2), "n": len(other), "z": round((v - m) / sd, 2),
                                       "note": "Compared with your previous days from a different phase. Information only: phase shifts are expected."}
        return out
    mean = statistics.fmean(vals)
    sd = max(statistics.stdev(vals) if len(vals) > 1 else 0.0, ind.get("baseline_min_sd", 0.0))
    z = (v - mean) / sd if sd else 0.0
    adverse = z >= Z_PERSONAL if bad == "high" else z <= -Z_PERSONAL if bad == "low" else abs(z) >= Z_PERSONAL
    pts = [(r["day"], r["value"]) for r in prior] + [(ctx.day, v)]
    per_week = slope(pts) * 7
    worsening = (per_week / sd >= 1 if bad == "high" else per_week / sd <= -1 if bad == "low" else abs(per_week) / sd >= 1)
    where = "above" if z > 0 else "below"
    out.update(scored=True, mean=round(mean, 3), sd=round(sd, 3), z=round(z, 2), status="watch" if adverse else "ok",
               trend_per_week=round(per_week, 3), worsening_trend=bool(worsening),
               sd_floor=ind.get("baseline_min_sd"),
               message=(f"{v:g} is {abs(z):.1f} SD {where} your own last {len(vals)} {ctx.phase} days "
                        f"(mean {mean:.2f}, SD {sd:.2f}); " + ("outside your usual range." if adverse else "within your usual range.")))
    if worsening:
        out["message"] += f" Trend over the window: {per_week:+.2f}/week in the adverse direction (information only)."
    return out


def phase_notes(ind, ctx):
    pn = ind.get("phase_notes") or {}
    return [pn[k] for k in (ctx.phase, "all") if k in pn]


def eval_indicator(S, grp, iid, ctx, v):
    ind = GROUPS[grp]["indicators"][iid]
    checks, unscored = [], []
    checks += check_cited(ind, v, ctx)
    w, u = check_windows(S, grp, iid, ind, ctx)
    checks += w
    unscored += u
    checks += check_labels(ind, v)
    checks += check_uncited(ind, v, ctx)
    ref = check_reference(ind, v, ctx)
    if ref and ref.get("applies") and ref.get("status") and not ref.get("info_only"):
        checks.append(chk("cohort_reference", ref["status"], ref["message"],
                          ind.get("context_action") if ref["status"] != "ok" else None, ref.get("sources"), True,
                          "nasa_cohort_reference_not_universal_limit"))
    pers = check_personal(S, grp, iid, ind, v, ctx)
    if pers and pers.get("scored"):
        checks.append(chk("personal_baseline", pers["status"], pers["message"],
                          GENERIC_ACTION if pers["status"] != "ok" else None, [], False, "app_level_baseline"))
    elif pers:
        unscored.append({"check": "personal_baseline", "status": "no_data", "message": pers["reason"]})
    delta = check_delta_phase(S, grp, iid, ind, v, ctx)

    scored = [c for c in checks]
    status = NAMES[max(SEV[c["status"]] for c in scored)] if scored else \
        ("context" if ind.get("baseline_bad") in (None, "none") and not ind.get("label_passthrough") else "no_data")
    wrong = [c["message"] for c in checks if c["status"] != "ok"]
    todo = []
    for c in checks:
        if c["status"] != "ok" and c.get("action") and c["action"] not in todo:
            todo.append(c["action"])
    notes = phase_notes(ind, ctx)
    if ind.get("no_nasa_reference"):
        notes.append(ind["no_nasa_reference"])
    lm = ind.get("late_mission_note")
    if lm and ctx.flight_day is not None and ctx.flight_day >= lm["mission_day_min"] and status != "ok":
        notes.append(lm["message"])
    if ind.get("provisional_note"):
        notes.append(ind["provisional_note"])
    used = []
    for c in checks:
        used += c["sources"]
    if ref and ref.get("sources"):
        used += ref["sources"]
    if delta and delta.get("sources"):
        used += delta["sources"]
    return {"group": grp, "indicator": iid, "label": ind["label"], "unit": ind["unit"], "value": v if ind["type"] != "bool" else bool(v),
            "status": status, "what_is_wrong": wrong, "what_to_do": todo, "checks": checks, "not_scored": unscored,
            "cohort_reference": ref, "personal_baseline": pers, "phase_shift_info": delta, "notes": notes,
            "measurement_context": ind.get("measurement_context"), "source_ids": sorted(set(used)),
            "reference_status": ind.get("reference_audit_status")}


# ------------------------------------------------------------------ compound predictions
def window_count(S, grp, iid, ctx, win):
    rows = series(S, grp, iid, ctx.day - win["days"] + 1, ctx.day)
    by_day = {r["day"]: r["value"] for r in rows}
    if not by_day:
        return None
    stat = statistics.fmean(by_day.values()) if win["stat"] == "mean" else sum(1 for x in by_day.values() if x)
    return op_test(win["op"], stat, win["value"])


def eval_compounds(S, grp, ctx, results, today_vals, xres=None):
    """results: {indicator: eval result}; today_vals: {indicator: value}; xres: results for indicators owned by another
    group (same person, same day). Returns (fired, not_evaluated)."""
    alias = COMPOUND_ALIAS.get(grp, {})
    inds = GROUPS[grp]["indicators"]
    xres = xres or {}

    def res_for(name):
        name = alias.get(name, name)
        if name in inds:
            return name, results.get(name)
        return name, xres.get(name)

    def clause(c):
        if "when" in c:
            return ctx.phase in c["when"]["phase"], None
        if "any_of" in c:
            states = [clause(x) for x in c["any_of"]]
            if any(s is True for s, _ in states):
                return True, None
            return (None if any(s is None for s, _ in states) else False), None
        name, r = res_for(c["indicator"])
        if "window" in c:
            if name not in inds:
                return None, name
            s = window_count(S, grp, name, ctx, c["window"])
            return s, name
        if r is None:
            return None, name
        if "status_in" in c:
            return r["status"] in c["status_in"], name
        if c.get("baseline_bad"):
            p = r.get("personal_baseline")
            return ((p["status"] != "ok") if p and p.get("scored") else None), name
        if "value" in c:
            return bool(r["value"]) == c["value"], name
        if c.get("below_prescribed_or_baseline"):
            p = r.get("personal_baseline")
            return ((p["status"] != "ok") if p and p.get("scored") else None), name
        if c.get("below_prescribed_sessions"):
            pres = today_vals.get("exercise_sessions_prescribed")
            return ((r["value"] < pres) if pres is not None else None), name
        if c.get("posture_response_abnormal"):
            ref = r.get("cohort_reference")
            return ((ref.get("status") != "ok") if ref and ref.get("applies") and ref.get("z") is not None and not ref.get("info_only") else None), name
        if c.get("window_average_elevated"):
            for ch in r["checks"]:
                if ch["check"].startswith("window:") and ch["status"] != "ok":
                    return True, name
            return (r["value"] >= 3.0), name
        if c.get("context_present"):
            return True, name
        return None, name

    def names_in(c):
        if "any_of" in c:
            return [n for x in c["any_of"] for n in names_in(x)]
        return [alias.get(c["indicator"], c["indicator"])] if "indicator" in c else []

    fired, pending = [], []
    for cr in GROUPS[grp].get("compound_rules", []):
        involved = {n for c in cr["requires"] for n in names_in(c)}
        if not (involved & set(results)):
            continue
        states = [clause(c)[0] for c in cr["requires"]]
        if all(s is True for s in states):
            fired.append({"id": cr["id"], "title": cr["title"], "status": cr["status"], "possible_concern": cr["possible_concern"],
                          "message": cr["message"], "what_to_do": cr["action"], "evidence_level": cr["evidence_level"],
                          "source_ids": cr["sources"], "threshold_note": cr.get("threshold_note"), "rule_group": grp})
        elif not any(s is False for s in states) and (involved & set(results)):
            pending.append({"id": cr["id"], "title": cr["title"],
                            "reason": "Not all required measurements/baselines are available for this day, so the rule was not evaluated (missing data is not treated as normal)."})
    return fired, pending


# ------------------------------------------------------------------ person context (motion sickness notes)
def person_context(grp, profile):
    out = []
    for n in GROUPS[grp].get("context_notes", []):
        w = n["when"]
        if w.get("always") or all(profile.get(k) == v for k, v in w.items()):
            out.append({"id": n["id"], "message": n["message"], "source_ids": n["sources"]})
    return out


# ------------------------------------------------------------------ main entry points
def day_values(S, grp, day, posture=""):
    """Every stored value of one group on one day: unposture-specific ones plus those taken in this posture."""
    out = {}
    for (g, iid), rows in S.items():
        if g != grp:
            continue
        for r in rows:
            if r["day"] == day and r["posture"] in ("", posture):
                out[iid] = r["value"]
    return out


def postures_on_day(S, grp, day):
    posts = {r["posture"] for (g, _), rows in S.items() if g == grp for r in rows if r["day"] == day}
    return sorted(p for p in posts if p) or [""]


def foreign_results(sid, S, grp, day, phase, detail, dsl, ctx):
    """Evaluate, for this person and this day, the indicators that this group's compound rules name but that belong to
    another group (e.g. cabin_co2_mmHg for the crew CO2 rule). Only values already stored/overlaid for the day count."""
    wanted = set()

    def walk(cl):
        for c in cl:
            if "any_of" in c:
                walk(c["any_of"])
            elif "indicator" in c:
                wanted.add(c["indicator"].lower())
    for cr in GROUPS[grp].get("compound_rules", []):
        walk(cr["requires"])
    out = {}
    for n in wanted:
        g2, iid = OWNER.get(n, (None, None))
        if g2 is None or g2 == grp or iid in GROUPS[grp]["indicators"]:
            continue
        rows = [r for r in S.get((g2, iid), []) if r["day"] == day]
        if not rows:
            continue
        r = next((x for x in rows if x["posture"] == (ctx.posture or "")), rows[-1])
        c2 = build_ctx(sid, S, day, phase, detail, r["posture"], dsl, ctx.profile, ctx.include_uncited)
        out[iid] = eval_indicator(S, g2, iid, c2, r["value"])
    return out


def evaluate(sid, S, grp, day, phase, detail, readings, posture="", dsl=None, profile=None, include_uncited=True):
    """readings: {indicator_id: float}. Evaluates those readings against rules, reference, own history and compounds."""
    ctx = build_ctx(sid, S, day, phase, detail, posture, dsl, profile if profile is not None else get_profile(sid), include_uncited)
    results = {iid: eval_indicator(S, grp, iid, ctx, v) for iid, v in readings.items()}
    fired, pending = eval_compounds(S, grp, ctx, results, readings, foreign_results(sid, S, grp, day, phase, detail, dsl, ctx))
    statuses = [r["status"] for r in results.values() if r["status"] in SEV] + [f["status"] for f in fired]
    overall = NAMES[max(SEV[s] for s in statuses)] if statuses else "no_data"
    flagged = [i for i, r in results.items() if r["status"] in ("watch", "act")]
    headline = {"ok": "All entered indicators are within their expected ranges.",
                "no_data": "Nothing could be scored (see not_scored for why)."}.get(
        overall, f"{overall.upper()}: " + ", ".join(flagged + [f["id"] for f in fired]))
    used = {s for r in results.values() for s in r["source_ids"]} | {s for f in fired for s in f["source_ids"]}
    pc = person_context(grp, ctx.profile)
    for p in pc:
        used |= set(p["source_ids"])
    missing_today = []
    for (g, iid), rows in S.items():
        if g == grp and iid not in readings and GROUPS[grp]["indicators"][iid].get("baseline_bad") in ("low", "high", "both") \
                and not any(r["day"] == day for r in rows) and any(r["day"] < day for r in rows):
            missing_today.append(iid)
    return {"subject_id": sid, "group": grp, "day": day, "phase": phase, "detail_phase": detail, "posture": posture or None,
            "days_since_landing": ctx.dsl, "flight_day": ctx.flight_day,
            "overall": {"status": overall, "headline": headline},
            "indicators": results, "predictions": fired, "predictions_not_evaluated": pending,
            "person_context": pc, "missing_today": sorted(missing_today), "notes": ctx.notes,
            "sources": {s: src(s) for s in sorted(used)}, "caveat": CAVEAT}


def parse_entry(group, readings):
    out = {}
    for name, raw in readings.items():
        if raw is None or raw == "":
            continue
        iid = resolve_indicator(group, name)
        out[iid] = clean_value(group, iid, raw)
    if not out:
        raise ValueError("no readings supplied")
    return out


def rule_names(cr):
    """Lower-cased indicator names a compound rule requires."""
    out = set()

    def walk(cl):
        for c in cl:
            if "any_of" in c:
                walk(c["any_of"])
            elif "indicator" in c:
                out.add(c["indicator"].lower())
    walk(cr["requires"])
    return out


def run_check(subject_id, group, day, phase, readings, posture=None, dsl=None, hour=None, profile=None, save=True, include_uncited=True):
    init_db()
    vals = parse_entry(group, readings)
    coarse = norm_phase(phase)
    if posture is not None and posture not in ("supine", "standing"):
        raise ValueError("posture must be 'supine' or 'standing'")
    if profile:
        set_profile(subject_id, profile)
    rows = [{"subject_id": subject_id, "grp": group, "indicator": i, "day": day, "phase": coarse, "detail_phase": str(phase).lower(),
             "posture": posture or "", "value": v, "hour": hour} for i, v in vals.items()]
    S = overlay(load_subject(subject_id), rows)
    # evaluate the whole day (earlier entries of the same day count for compound predictions)
    out = evaluate(subject_id, S, group, day, coarse, str(phase).lower(), day_values(S, group, day, posture or ""),
                   posture or "", dsl, None, include_uncited)
    out["entered_indicators"] = sorted(vals)
    seen = {p["id"] for p in out["predictions"]}
    pend = {p["id"] for p in out["predictions_not_evaluated"]}
    for g2 in GROUPS:                       # rules of other groups that use the readings just entered
        if g2 == group or not GROUPS[g2].get("compound_rules"):
            continue
        mine = {i.lower() for i in GROUPS[group]["indicators"]}
        mine_rules = {cr["id"] for cr in GROUPS[g2]["compound_rules"] if rule_names(cr) & mine}
        if not mine_rules:
            continue
        posts = postures_on_day(S, g2, day)
        for post in posts:
            vals2 = day_values(S, g2, day, post)
            if not vals2:
                continue
            o2 = evaluate(subject_id, S, g2, day, coarse, str(phase).lower(), vals2, post, dsl, None, include_uncited)
            for f in o2["predictions"]:
                if f["id"] in mine_rules and f["id"] not in seen:
                    out["predictions"].append(f); seen.add(f["id"])
            for f in o2["predictions_not_evaluated"]:
                if f["id"] in mine_rules and f["id"] not in pend and f["id"] not in seen:
                    out["predictions_not_evaluated"].append(f); pend.add(f["id"])
    if out["predictions"]:                  # a cross-group finding can raise the overall status
        worst = max(SEV[p["status"]] for p in out["predictions"])
        if worst > SEV.get(out["overall"]["status"], -1):
            out["overall"] = {"status": NAMES[worst], "headline": out["overall"]["headline"]}
    if save:
        upsert(rows)
    out["saved"] = save
    return out


def status_for_day(sid, day=None, group=None, include_uncited=True):
    S = load_subject(sid)
    if not S:
        return None
    days = sorted({r["day"] for rows in S.values() for r in rows})
    day = days[-1] if day is None else day
    out = {"subject_id": sid, "day": day, "groups": {}}
    for grp in GROUPS:
        if group and grp != group:
            continue
        meta = next((r for (g, _), rows in S.items() if g == grp for r in rows if r["day"] == day), None)
        if meta is None:
            continue
        # one evaluation per posture present that day (mixing postures would corrupt baselines)
        res = [evaluate(sid, S, grp, day, meta["phase"], meta["detail_phase"], day_values(S, grp, day, post), post, None, None, include_uncited)
               for post in postures_on_day(S, grp, day)]
        out["groups"][grp] = res[0] if len(res) == 1 else res
    sts = []
    for v in out["groups"].values():
        for e in (v if isinstance(v, list) else [v]):
            sts.append(e["overall"]["status"])
    ranked = [s for s in sts if s in SEV]
    out["overall"] = NAMES[max(SEV[s] for s in ranked)] if ranked else "no_data"
    return out


def timeline(sid, group=None):
    S = load_subject(sid)
    days = sorted({r["day"] for rows in S.values() for r in rows})
    rows = []
    for d in days:
        st = status_for_day(sid, d, group)
        if not st or not st["groups"]:
            continue
        for grp, v in st["groups"].items():
            for e in (v if isinstance(v, list) else [v]):
                rows.append({"day": d, "group": grp, "phase": e["detail_phase"], "status": e["overall"]["status"],
                             "flagged": [i for i, r in e["indicators"].items() if r["status"] in ("watch", "act")],
                             "predictions": [p["id"] for p in e["predictions"]],
                             "unscored": sum(len(r["not_scored"]) for r in e["indicators"].values())})
    return rows


def baselines(sid, day=None, group=None):
    S = load_subject(sid)
    if not S:
        return None
    days = sorted({r["day"] for rows in S.values() for r in rows})
    day = days[-1] if day is None else day
    out = []
    for (grp, iid), rows in sorted(S.items()):
        if group and grp != group:
            continue
        ind = GROUPS[grp]["indicators"][iid]
        cur = [r for r in rows if r["day"] <= day]
        if not cur:
            continue
        last = cur[-1]
        ctx = build_ctx(sid, S, last["day"], last["phase"], last["detail_phase"], last["posture"])
        p = check_personal(S, grp, iid, ind, last["value"], ctx)
        if p:
            out.append({"group": grp, "indicator": iid, "latest_day": last["day"], "latest_value": last["value"], **p})
    return out


# ------------------------------------------------------------------ CSV loader
def load_csv(path, subject=None, reset=False):
    init_db()
    if reset:
        with conn() as c:
            c.execute("DELETE FROM observations")
            c.execute("DELETE FROM subjects")
    rows, prof, per = [], {}, {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames or []
        colmap = {c: CSV_MAP[c.strip().lower()] for c in cols if c.strip().lower() in CSV_MAP}
        unmapped = [c for c in cols if c not in colmap and c not in CSV_META]
        for r in rd:
            sid = subject or r["astronaut_id"]
            day, detail = int(float(r["mission_day"])), r["mission_phase"].strip().lower()
            phase = norm_phase(detail)
            posture = (r.get("posture") or "").strip().lower()
            if posture not in ("", "supine", "standing"):
                per.setdefault("rejected", []).append(f"{sid} day {day}: posture '{posture}' is not supine/standing")
                posture = ""
            p = prof.setdefault(sid, {})
            for col, key in CSV_PROFILE.items():
                if r.get(col) not in (None, ""):
                    if key == "age":
                        try:
                            p[key] = float(str(r[col]).replace(" ", ""))   # tolerate typos such as "4 5"
                        except ValueError:
                            per.setdefault("rejected", []).append(f"{sid} day {day}: age '{r[col]}' is not a number")
                    else:
                        p[key] = r[col]
            for col, (grp, iid) in colmap.items():
                raw = r.get(col)
                if raw in (None, ""):
                    continue
                try:
                    v = clean_value(grp, iid, raw)
                except ValueError as e:
                    per.setdefault("rejected", []).append(f"{sid} day {day}: {e}")
                    continue
                rows.append({"subject_id": sid, "grp": grp, "indicator": iid, "day": day, "phase": phase,
                             "detail_phase": detail, "posture": posture if grp == "crew" else "", "value": v, "hour": None})
    upsert(rows)
    for sid, p in prof.items():
        set_profile(sid, p)
    return {"subjects": sorted(prof), "observations": len(rows), "unmapped_columns": unmapped,
            "rejected": per.get("rejected", []), "assumptions": CSV_ASSUMPTIONS}


# ------------------------------------------------------------------ API
def make_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field

    class CheckIn(BaseModel):
        subject_id: str
        group: str = Field("crew", description="crew | motion_sickness | body_temperature")
        day: int = Field(..., ge=0, description="mission day number (the same numbering as the person's earlier entries)")
        phase: str = Field(..., description="preflight | inflight | landing_day | postflight (aliases such as in_flight_cruise accepted)")
        readings: Dict[str, Any] = Field(..., description="indicator id or alias -> value; yes/no flags as true/false")
        posture: Optional[str] = Field(None, description="supine | standing: needed for the posture-specific NASA HR/BP reference")
        days_since_landing: Optional[int] = Field(None, ge=0)
        hour: Optional[float] = Field(None, ge=0, le=24)
        profile: Optional[Dict[str, Any]] = Field(None, description="person fields, e.g. first_flight, past_motion_sickness, lunar_landing")
        save: bool = True
        include_uncited: bool = True

    class Profile(BaseModel):
        profile: Dict[str, Any]

    app = FastAPI(title="ASTRA PULSE health check", version="2.0",
                  description="Rule engine driven by astra_rules.json v5. " + CAVEAT)
    app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("ASTRA_CORS", "*").split(","),
                       allow_methods=["*"], allow_headers=["*"])
    init_db()

    @app.get("/")
    def root():
        m = RULES["meta"]
        return {"name": "ASTRA PULSE backend", "rules_version": m["version"], "rules_updated": m["updated"],
                "groups": {g: {"label": v["label"], "indicators": len(v["indicators"])} for g, v in GROUPS.items()},
                "status_values": m["status_values"], "safety_note": m["safety_note"], "docs": "/docs"}

    @app.get("/indicators")
    def indicators(group: Optional[str] = None):
        out = {}
        for g, v in GROUPS.items():
            if group and g != group:
                continue
            out[g] = {iid: {"label": i["label"], "unit": i["unit"], "type": i["type"], "sanity_range": i.get("sanity_range"),
                            "aliases": i.get("aliases", []), "adverse_direction": i.get("baseline_bad"),
                            "reference_status": i.get("reference_audit_status")} for iid, i in v["indicators"].items()}
        if group and not out:
            raise HTTPException(404, f"unknown group {group}")
        return out

    @app.get("/indicators/{group}/{indicator}")
    def indicator(group: str, indicator: str):
        try:
            iid = resolve_indicator(group, indicator)
        except ValueError as e:
            raise HTTPException(404, str(e))
        ind = GROUPS[group]["indicators"][iid]
        ids = set(ind.get("audit_sources", [])) | set(ind.get("extra_sources", []))
        return {**{k: v for k, v in ind.items() if k not in ("audit_sources", "extra_sources")}, "sources": [src(s) for s in sorted(ids)]}

    @app.get("/sources")
    def sources():
        return {s: src(s) for s in SOURCES}

    @app.post("/check")
    def check(body: CheckIn):
        try:
            return run_check(body.subject_id, body.group, body.day, body.phase, body.readings, body.posture,
                             body.days_since_landing, body.hour, body.profile, body.save, body.include_uncited)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/subjects")
    def subjects():
        return list_subjects()

    @app.put("/subjects/{sid}/profile")
    def put_profile(sid: str, body: Profile):
        init_db()
        return set_profile(sid, body.profile)

    @app.get("/subjects/{sid}/status")
    def status(sid: str, day: Optional[int] = None, group: Optional[str] = None, include_uncited: bool = True):
        r = status_for_day(sid, day, group, include_uncited)
        if r is None:
            raise HTTPException(404, f"no data for {sid}")
        return r

    @app.get("/subjects/{sid}/timeline")
    def tl(sid: str, group: Optional[str] = None):
        if sid not in list_subjects():
            raise HTTPException(404, f"no data for {sid}")
        return timeline(sid, group)

    @app.get("/subjects/{sid}/baseline")
    def bl(sid: str, day: Optional[int] = None, group: Optional[str] = None):
        r = baselines(sid, day, group)
        if r is None:
            raise HTTPException(404, f"no data for {sid}")
        return r

    @app.get("/subjects/{sid}/history")
    def history(sid: str, indicator: Optional[str] = None, group: Optional[str] = None):
        S = load_subject(sid)
        if not S:
            raise HTTPException(404, f"no data for {sid}")
        return [{"group": g, "indicator": i, "day": r["day"], "phase": r["detail_phase"], "posture": r["posture"] or None, "value": r["value"]}
                for (g, i), rows in sorted(S.items()) if (not group or g == group) and (not indicator or i == indicator) for r in rows]

    ui = os.path.join(HERE, "astra-frontend")
    if os.path.isdir(ui):                       # open http://127.0.0.1:8000/ui/ : page and API share one origin
        from fastapi.staticfiles import StaticFiles
        app.mount("/ui", StaticFiles(directory=ui, html=True), name="ui")

    return app


# ------------------------------------------------------------------ CLI
def print_check(out):
    print(f"\n{out['subject_id']} | {out['group']} | day {out['day']} | {out['phase']} ({out['detail_phase']})"
          + (f" | flight day {out['flight_day']}" if out["flight_day"] else "") + f"  =>  {out['overall']['status'].upper()}")
    for iid, r in out["indicators"].items():
        print(f"  [{r['status']:7s}] {r['label']} = {r['value']} {r['unit']}")
        for m in r["what_is_wrong"]:
            print(f"      wrong : {m}")
        for a in r["what_to_do"]:
            print(f"      do    : {a}")
        for u in r["not_scored"]:
            print(f"      (not scored) {u['message']}")
    for p in out["predictions"]:
        print(f"  PREDICTION [{p['status']}] {p['possible_concern']}: {p['message']}\n      do    : {p['what_to_do']}")
    for p in out["predictions_not_evaluated"]:
        print(f"  (prediction not evaluated) {p['title']}")
    if out["missing_today"]:
        print("  missing today (not treated as normal): " + ", ".join(out["missing_today"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="ASTRA PULSE backend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    l = sub.add_parser("load"); l.add_argument("csv"); l.add_argument("--subject"); l.add_argument("--reset", action="store_true")
    r = sub.add_parser("replay"); r.add_argument("subject"); r.add_argument("--group")
    c = sub.add_parser("check")
    c.add_argument("--subject", required=True); c.add_argument("--group", default="crew"); c.add_argument("--day", type=int, required=True)
    c.add_argument("--phase", required=True); c.add_argument("--posture"); c.add_argument("--no-save", action="store_true")
    c.add_argument("--set", action="append", default=[], metavar="indicator=value")
    sub.add_parser("indicators")
    a = ap.parse_args(argv)
    init_db()
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run(make_app(), host=a.host, port=a.port)
    elif a.cmd == "load":
        info = load_csv(a.csv, a.subject, a.reset)
        print(f"loaded {info['observations']} observations for {', '.join(info['subjects'])}")
        print("not loaded:", ", ".join(info["unmapped_columns"]))
        for x in info["rejected"]:
            print("rejected:", x)
        print("mapping assumptions:"); [print("  -", x) for x in info["assumptions"]]
    elif a.cmd == "replay":
        rows = timeline(a.subject, a.group)
        if not rows:
            sys.exit("no data")
        print(f"{'day':>3} {'group':<18}{'phase':<31}{'status':<8}flagged / predictions")
        for x in rows:
            print(f"{x['day']:>3} {x['group']:<18}{x['phase']:<31}{x['status']:<8}{', '.join(x['flagged'] + x['predictions'])}")
    elif a.cmd == "check":
        kv = dict(item.split("=", 1) for item in a.set)
        try:
            print_check(run_check(a.subject, a.group, a.day, a.phase, kv, a.posture, save=not a.no_save))
        except ValueError as e:
            sys.exit(f"error: {e}")
    elif a.cmd == "indicators":
        for g, v in GROUPS.items():
            print(f"\n{g}: {v['label']}")
            for iid, i in v["indicators"].items():
                print(f"  {iid:<42}{i['unit']:<12}aliases: {', '.join(i.get('aliases', []))}")


def __getattr__(name):                  # `uvicorn astra_backend:app` builds the app on demand; importing has no side effects
    if name == "app":
        return make_app()
    raise AttributeError(name)


if __name__ == "__main__":
    main()
