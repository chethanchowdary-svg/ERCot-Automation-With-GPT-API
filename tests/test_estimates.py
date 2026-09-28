import datetime as dt

from ercot_qb import estimates as E
from ercot_qb import field_rules as FR
from ercot_qb import rules as R

RUN = dt.date(2026, 9, 23)


def test_fc_early_min_and_cap():
    assert E.estimate_fc_dev(R.EARLY, 10, RUN) == dt.date(2027, 3, 31)      # 6-month floor
    assert E.estimate_fc_dev(R.EARLY, 100, RUN) == dt.date(2028, 1, 31)     # 16 months
    assert E.estimate_fc_dev(R.EARLY, 1500, RUN) == dt.date(2029, 9, 30)    # 3-year cap


def test_fc_late_and_cod():
    fc = E.estimate_fc_dev(R.LATE, 162.1, RUN)
    assert fc == dt.date(2028, 4, 30)                                       # 19 months
    assert E.estimate_cod_from_fc(fc, 162.1, "Wind") == dt.date(2029, 11, 30)
    assert E.estimate_cod_from_fc(dt.date(2027, 12, 31), 1000, "Solar") == dt.date(2031, 12, 31)  # 4-yr cap
    assert E.estimate_cod_from_fc(dt.date(2027, 12, 31), 10, "Wind") == dt.date(2028, 6, 30)      # wind min 6


def test_backdate_operational():
    assert E.estimate_fc_from_cod(dt.date(2026, 8, 13), 203.5, "Solar") == dt.date(2024, 8, 31)
    assert E.estimate_fc_from_cod(dt.date(2026, 8, 1), 2000, "Solar") == dt.date(2023, 8, 31)     # 3-yr cap


def test_investment_tiers():
    assert E.estimate_investment("Wind", 50, R.EARLY, None, RUN) == (80, 1.6)
    assert E.estimate_investment("Solar", 500, R.LATE, None, RUN) == (500, 1.0)
    assert E.estimate_investment("Battery", 100, R.EARLY, None, RUN) == (100, 1.0)
    assert E.estimate_investment("Wind", 100, R.OPER, dt.date(2012, 1, 1), RUN)[1] == 1.8


def test_timeline_replaced_in_place():
    old = "Other:\r\n-- a\r\n\r\nEstimated development timeline:\r\n-- x\r\n\r\nPPA:\r\n-- y"
    new = E.replace_timeline_in_place(old, "Estimated Development Timeline:\n-- z")
    assert new == "Other:\r\n-- a\r\n\r\nEstimated Development Timeline:\r\n-- z\r\n\r\nPPA:\r\n-- y"


def test_field_rules():
    base = {"Sector": "Battery", "Project life cycle": R.OPER, "PPA signed": "No",
            "Revenue mechanism": "Pending finalization", "Power sales arrangement": "",
            "Financing secured": "Yes - Complete", "Financing type": "Debt"}
    out = FR.apply({**base, **{c: "" for t in FR.STATUS_COLS for c in t}})
    assert out["PPA signed"] == "Yes - Complete"
    assert out["Revenue mechanism"] == FR.YTBD
    assert out["Power sales arrangement"] == "Not Applicable"
    cs = FR.apply({"Sector": "Solar", "Project life cycle": R.CANCELLED, "Financing secured": "No",
                   "Financing type": "Pending finalization",
                   **{c: "x" for t in FR.STATUS_COLS for c in t}})
    assert cs["Financial close date status"] == R.NR and cs["Operational date (reported)"] == ""


def test_plan_timeline_rules():
    run = dt.date(2026, 9, 25)
    # 300 MW early-stage: FC capped at 3 years, COD from FC
    assert E.plan_timeline(R.EARLY, "Solar", 300, run, "Estimated", None, "Estimated", None, None)[:4] == \
        ("Estimated", dt.date(2029, 9, 30), "Estimated", dt.date(2032, 9, 30))
    # past reported FC is dropped; FC re-estimated and fitted before the reported COD
    fs, fc, os_, op, notes = E.plan_timeline(R.EARLY, "Solar", 300, run, "Reported", dt.date(2025, 1, 1),
                                             "Reported", dt.date(2028, 6, 30), None)
    assert (fs, fc, os_, op) == ("Estimated", dt.date(2028, 3, 31), "Reported", dt.date(2028, 6, 30)) and notes
    # reported COD too soon for an early-stage project is re-estimated
    assert E.plan_timeline(R.EARLY, "Solar", 300, run, "", None, "Reported", dt.date(2026, 12, 31), None)[2] == "Estimated"
    # in construction: FC in the past, COD in the future
    fs, fc, os_, op, _ = E.plan_timeline("In construction", "Solar", 200, run, "Reported", dt.date(2025, 3, 1),
                                         "Estimated", None, None)
    assert fc < run < op
