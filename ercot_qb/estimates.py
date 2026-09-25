"""FC / COD / investment estimation and analyst-comment text (SOP sections 'estimations')."""
from __future__ import annotations

import calendar
import datetime as dt
import math
import re

from . import rules as R

INV_HDR = re.compile(r"^\W*e?stimated\s+project\s+investment\s*:", re.I)
TL_HDR = re.compile(r"^\W*e?stimated\s+development\s+timeline\s*:", re.I)


# ---------------------------------------------------------------- dates
def eom(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months_eom(d: dt.date, months: int) -> dt.date:
    idx = d.year * 12 + (d.month - 1) + months
    y, m = divmod(idx, 12)
    return dt.date(y, m + 1, calendar.monthrange(y, m + 1)[1])


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def months_for(capacity: float, per_50: int) -> int:
    return round_half_up((capacity or 0) / 50 * per_50)


def cod_min(sector: str) -> int:
    return R.COD_MIN_MONTHS.get(sector, R.COD_MIN_DEFAULT)


def estimate_fc_dev(stage: str, capacity: float, run: dt.date) -> dt.date:
    stage = stage if stage in R.FC_PER_50MW else R.EARLY
    m = max(months_for(capacity, R.FC_PER_50MW[stage]), R.FC_MIN_MONTHS[stage])
    m = min(m, R.FC_MAX_MONTHS)
    return add_months_eom(run, m)


def estimate_cod_from_fc(fc: dt.date, capacity: float, sector: str) -> dt.date:
    m = months_for(capacity, R.COD_PER_50MW)
    m = min(max(m, cod_min(sector)), R.COD_MAX_MONTHS)
    return add_months_eom(fc, m)


def estimate_fc_from_cod(cod: dt.date, capacity: float, sector: str) -> dt.date:
    m = months_for(capacity, R.BACKDATE_PER_50MW)
    m = min(max(m, cod_min(sector)), R.BACKDATE_MAX_MONTHS)  # DEFAULT: floor = min build time
    return add_months_eom(cod, -m)


# ---------------------------------------------------------------- investment
def dev_metric(sector: str, capacity: float) -> float:
    lo, hi = R.CAP_TIERS
    tiers = R.DEV_METRIC.get(sector, R.DEV_METRIC["Battery"])
    cap = capacity or 0
    return tiers[0] if cap < lo else tiers[1] if cap < hi else tiers[2]


def oper_metric(sector: str, cod: dt.date | None, run: dt.date) -> float:
    tiers = R.OPER_METRIC.get(sector, R.OPER_METRIC["Battery"])
    if cod is None:
        return tiers[0]
    age = (run - cod).days / 365.25
    return tiers[0] if age < 5 else tiers[1] if age <= 10 else tiers[2]


def estimate_investment(sector, capacity, lifecycle, cod, run):
    metric = oper_metric(sector, cod, run) if lifecycle == R.OPER else dev_metric(sector, capacity)
    return max(1, round_half_up((capacity or 0) * metric)), metric


# ---------------------------------------------------------------- comments
def sector_comment_name(sector: str, subsector: str) -> str:
    return R.SECTOR_COMMENT_NAME.get((sector, subsector), (sector or "").lower())


def mmmm_yyyy(d: dt.date) -> str:
    return d.strftime("%B %Y")


def investment_section(inv: float, metric: float, secname: str) -> str:
    return ("Estimated Project Investment:\n"
            f"-- Enerdatics calculates the project's investment to be ${inv:,.0f} million, "
            f"representing an investment metric of ${metric:.2f} million/MW.\n"
            f"-- This estimate is in line with reported figures for {secname} projects of this "
            "scale and size in the region.")


def timeline_section(fc_status, fc, op_status, op, lifecycle, secname) -> str | None:
    basis = (f"-- This estimate is based on reported development timelines for {secname} "
             "projects of this scale and size in the region.")
    if fc_status == "Reported" and op_status == "Reported":
        return None
    if fc_status == "Reported" and op_status == "Estimated":              # Case 2
        return ("Estimated Development Timeline:\n"
                f"-- The project is estimated to be commercially operational by {mmmm_yyyy(op)}.\n"
                f"{basis}\n"
                "-- Further, the developer will secure financing to fund construction activities "
                f"on the project by {mmmm_yyyy(fc)}.")
    if op_status == "Reported" and fc_status == "Estimated":
        if lifecycle == R.OPER:                                            # Case 3
            return ("Estimated Development Timeline:\n"
                    f"-- The project reached commercial operations in {mmmm_yyyy(op)}.\n"
                    "-- Further, Enerdatics believes that developer would have secured financing "
                    f"to fund construction activities on the project by {mmmm_yyyy(fc)}.\n"
                    f"{basis}")
        # DEFAULT: dev project with reported COD, FC estimated (not covered by SOP)
        return ("Estimated Development Timeline:\n"
                "-- Enerdatics believes that the developer will have secured financing to fund "
                f"construction activities on the project by {mmmm_yyyy(fc)}.\n"
                f"{basis}")
    if fc_status == "Estimated" and op_status == "Estimated":            # Case 4
        return ("Estimated Development Timeline:\n"
                f"-- The project is estimated to be commercially operational by {mmmm_yyyy(op)}.\n"
                "-- Further, Enerdatics believes that the developer will have secured financing "
                f"to fund construction activities on the project by {mmmm_yyyy(fc)}.\n"
                f"{basis}")
    return None


def split_sections(comment: str) -> list[str]:
    text = (comment or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"\n\s*\n", text) if s.strip()]


def rebuild_comment(existing: str, inv_sec: str | None, tl_sec: str | None,
                    replace_inv: bool, replace_tl: bool) -> str:
    """Keep analyst-written sections, swap in the auto sections that were re-estimated."""
    keep, old_inv, old_tl = [], None, None
    for s in split_sections(existing):
        if INV_HDR.match(s):
            old_inv = s
        elif TL_HDR.match(s):
            old_tl = s
        else:
            keep.append(s)
    inv = inv_sec if replace_inv else old_inv
    tl = tl_sec if replace_tl else old_tl
    return "\n\n".join([s for s in keep + [inv, tl] if s])


def strip_auto_sections(existing: str) -> str:
    return "\n\n".join(s for s in split_sections(existing) if not (INV_HDR.match(s) or TL_HDR.match(s)))


def replace_timeline_in_place(existing: str, tl_sec: str | None) -> str:
    """QC rows: swap only the timeline paragraph, leave every other character as it was."""
    text = existing or ""
    nl = "\r\n" if "\r\n" in text else "\n"
    new = tl_sec.replace("\n", nl) if tl_sec else ""
    m = re.search(r"(?im)^[^\w\r\n]*e?stimated[ \t]+development[ \t]+timeline[ \t]*:.*?(?=(\r?\n[ \t]*\r?\n)|\Z)",
                  text, re.S)
    if m:
        out = text[:m.start()] + new + text[m.end():]
        if not new:  # drop the now-empty paragraph gap
            out = re.sub(r"(\r?\n[ \t]*){3,}", nl + nl, out).strip()
        return out
    if not new:
        return text
    return (text.rstrip() + nl + nl + new) if text.strip() else new
