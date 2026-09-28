"""Field-level rules applied to every in-scope row (new rows and ERCOT-matched existing rows).

User rules (Sep 2026):
- Battery: Power sales arrangement = Not Applicable (always).
  Early/Late: PPA signed = No (blank only); Revenue mechanism = Yet to be determined/Unavailable (blank only).
  Operational: PPA signed = Yes - Complete (overwrite); Revenue mechanism = Yet to be
  determined/Unavailable (blank only; "Pending finalization" counts as blank).
- Solar / Wind: Early/Late: PPA signed = No (blank only).
  Operational: PPA signed = Yes - Complete (overwrite); Power sales arrangement = Yet to be
  determined/Unavailable (blank only).
- Financing secured = No  ->  Financing type blank (never "Pending finalization").
- Status = Estimated  ->  value sits in the estimated column, reported column blank.
  Status = Reported   ->  value sits in the reported column, estimated column blank.
- Cancelled / Suspended: FC, operational and investment status = Not reported, need more data
  to estimate; estimated AND reported values cleared.
"""
from __future__ import annotations

from . import rules as R

PSA, PPA, REV = "Power sales arrangement", "PPA signed", "Revenue mechanism"
YTBD = "Yet to be determined/Unavailable"
STATUS_COLS = (
    ("Financial close date status", "Financial close (estimated)", "Financial close date (reported)"),
    ("Operational date status", "Operational date (estimated)", "Operational date (reported)"),
    ("Project investment status", "Project investment ($M) - Estimated", "Project investment ($M) - Reported"),
)
# DEFAULT: "Pending finalization" is a development-stage placeholder, so an Operational battery
# carrying it is treated as blank.
REV_PLACEHOLDERS = ("", "pending finalization")


def apply(m: dict) -> dict:
    """Return the field updates needed on merged row `m`."""
    out = {}
    g = lambda k: (m.get(k) or "").strip()
    sec, lc = g("Sector"), g("Project life cycle")

    if sec == "Battery":
        out[PSA] = "Not Applicable"
        if lc in (R.EARLY, R.LATE):
            if not g(PPA):
                out[PPA] = "No"
            if not g(REV):
                out[REV] = YTBD
        elif lc == R.OPER:
            out[PPA] = "Yes - Complete"
            if g(REV).lower() in REV_PLACEHOLDERS:
                out[REV] = YTBD
    elif sec in ("Solar", "Wind"):
        if lc in (R.EARLY, R.LATE):
            if not g(PPA):
                out[PPA] = "No"
        elif lc == R.OPER:
            out[PPA] = "Yes - Complete"
            if not g(PSA):
                out[PSA] = YTBD

    if g("Financing secured").lower() == "no" and g("Financing type"):
        out["Financing type"] = ""

    if lc in (R.CANCELLED, R.SUSPENDED):
        for st, est, rep in STATUS_COLS:
            out[st], out[est], out[rep] = R.NR, "", ""
        return out

    for st, est, rep in STATUS_COLS:
        if g(st) == "Estimated" and g(rep):
            if not g(est):
                out[est] = m[rep]
            out[rep] = ""
        elif g(st) == "Reported" and g(est):
            if not g(rep):
                out[rep] = m[est]
            out[est] = ""
    return out
