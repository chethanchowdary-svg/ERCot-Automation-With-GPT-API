import pandas as pd

from ercot_qb import id_matcher as M
from ercot_qb import rules as R
from ercot_qb.ercot_source import ErcotRec


def _db(rows):
    base = {c: "" for c in R.DB_COLUMNS}
    return pd.DataFrame([{**base, "Power market": "ERCOT", **r} for r in rows])


def _rec(inr, name, sector="Solar", mw=200.0, county="Bee"):
    return ErcotRec(inr=inr.upper(), inr_raw=inr, source="large_gen", name=name, county=county,
                    capacity=mw, sector=sector, subsector="")


NOID = _db([
    {"Project ID": "A-1", "Project name": "Abeja Solar Farm", "Sector": "Solar",
     "Power Capacity (MW)": "200", "Sub-location": "Bee"},
    {"Project ID": "A-2", "Project name": "Buffalo Gap wind farm 2", "Sector": "Wind",
     "Power Capacity (MW)": "232", "Sub-location": "Nolan"},
    {"Project ID": "A-3", "Project name": "Abeja BESS", "Sector": "Battery",
     "Power Capacity (MW)": "100", "Sub-location": "Bee"},
])


def always_first(payload, key, model):
    return {"match_project_id": payload["candidates"][0]["project_id"], "confidence": "high", "reason": "t"}


def test_shortlist_same_sector_only():
    noid = M.prepare_noid(NOID)
    c = M.shortlist(_rec("27INR0372", "Abeja Solar Farm"), noid)
    assert [noid.at[i, "Project ID"] for _, i in c] == ["A-1"]


def test_match_applied_and_guard_caps_repower(tmp_path):
    recs = [_rec("27INR0372", "Abeja Solar Farm"),
            _rec("26INR0625", "Buffalo Gap 2 Wind Repower", "Wind", 231.5, "Nolan")]
    applied, decs, stats = M.match(recs, NOID, None, api_key="k", ask=always_first)
    assert set(applied) == {"27INR0372"}
    capped = [d for d in decs if d.inr == "26INR0625"][0]
    assert capped.confidence == "medium" and not capped.applied
    assert stats["GPT calls"] == 2


def test_one_inr_per_db_project():
    recs = [_rec("27INR0372", "Abeja Solar Farm"), _rec("27INR0999", "Abeja Solar Farm", mw=150)]
    applied, _, _ = M.match(recs, NOID, None, api_key="k", ask=always_first)
    assert len(applied) == 1 and "27INR0372" in applied


def test_overrides(tmp_path):
    p = tmp_path / "ov.csv"
    p.write_text("Interconnection ID,Project ID,Decision,Note\n"
                 "27INR0372,A-1,reject,\n27INR0500,A-3,match,\n")
    recs = [_rec("27INR0372", "Abeja Solar Farm"), _rec("27INR0500", "Something Else", "Battery", 90)]
    applied, _, stats = M.match(recs, NOID, p, api_key="k", ask=always_first)
    assert applied == {"27INR0500": 2}
    assert stats["GPT calls"] == 0


def test_no_api_key_lists_candidates_only():
    applied, decs, _ = M.match([_rec("27INR0372", "Abeja Solar Farm")], NOID, None, api_key="",
                               ask=always_first)
    assert applied == {} and decs[0].project_id == "A-1" and decs[0].method == "none"
