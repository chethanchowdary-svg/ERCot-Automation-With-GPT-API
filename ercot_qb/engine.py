"""Compare ERCOT GIS report with the Quickbase table and build the three outputs."""
from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import estimates as E
from . import field_rules as FR
from . import id_matcher
from . import rules as R
from .ercot_source import ErcotRec, norm_inr, unify

LC, GCS, ISO, GCA, EIA = ("Project life cycle", "Grid connection status",
                          "Grid connection status (reported by ISO)", "Grid connection approved",
                          "EIA approval received")
FCS, FCR, FCE = "Financial close date status", "Financial close date (reported)", "Financial close (estimated)"
OPS, OPR, OPE = "Operational date status", "Operational date (reported)", "Operational date (estimated)"
INVS, INVE, INVR = ("Project investment status", "Project investment ($M) - Estimated",
                    "Project investment ($M) - Reported")
AC, SAC = "Analyst comments", "Show analyst comment"
IA_DATE, CAP = "Interconnection signed date", "Power Capacity (MW)"


# ---------------------------------------------------------------- helpers
def fmt_date(d):
    return d.strftime(R.DATE_FMT) if d else ""


def parse_db_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for f in (R.DATE_FMT, "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def fmt_num(x):
    if x is None:
        return ""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def num(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def same(a, b) -> bool:
    a, b = ("" if a is None else str(a)).strip(), ("" if b is None else str(b)).strip()
    if a.lower() == b.lower():
        return True
    na, nb = num(a), num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-6
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def yn(v: str) -> str:
    return "yes" if v else "no"


# ---------------------------------------------------------------- county lookup
class CountyLookup:
    FIELDS = ["Utility service area", "NERC Region", "NERC Sub-region", "ERCOT Weather Zone"]

    def __init__(self, csv_path: Path, db: pd.DataFrame):
        self.table = {}
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                self.table[r["Sub-location"].strip().lower()] = r
        self.db_mode = {}
        for county, g in db.groupby(db["Sub-location"].str.strip().str.lower()):
            self.db_mode[county] = {
                c: (g.loc[g[c] != "", c].value_counts().index[0] if (g[c] != "").any() else "")
                for c in self.FIELDS
            }
        self.utility_spelling = {u.lower(): u for u in db["Utility service area"].unique() if u}

    def get(self, county: str):
        k = (county or "").strip().lower()
        spec, dbm = self.table.get(k, {}), self.db_mode.get(k, {})
        out, used_fallback = {}, []
        for c in self.FIELDS:
            v = (spec.get(c) or "").strip()
            if not v and dbm.get(c):
                v = dbm[c]
                used_fallback.append(c)
            if c == "Utility service area":
                v = self.utility_spelling.get(v.lower(), v)
            out[c] = v
        return out, (k in self.table), used_fallback


# ---------------------------------------------------------------- result
@dataclass
class Result:
    updates: list = field(default_factory=list)
    new: list = field(default_factory=list)
    qc: list = field(default_factory=list)
    review: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    id_matches: list = field(default_factory=list)
    match_stats: dict = field(default_factory=dict)

    def flag(self, kind, rec: ErcotRec | None, row: dict | None, detail):
        self.review.append({
            "Type": "Flag", "Flag": kind,
            "Source sheet": R.SOURCE_LABEL.get(rec.source, "") if rec else "",
            "Interconnection ID": rec.inr_raw if rec else (row or {}).get("Interconnection ID", ""),
            "Project ID": (row or {}).get("Project ID", ""),
            "Project name": (row or {}).get("Project name", "") or (rec.name if rec else ""),
            "Lead QC": (row or {}).get("Lead QC", ""),
            "Detail": detail,
        })


# ---------------------------------------------------------------- existing rows
def propose(rec: ErcotRec, row: dict, res: Result):
    """Status-driven field changes for one matched DB row (before estimation)."""
    P, lc = {}, row[LC]

    def fill_spv_poi(src: ErcotRec | None):
        if not src:
            return
        if not row["SPV"].strip() and src.spv:
            P["SPV"], P["Show SPV"] = src.spv, "yes"
        if not row["POI"].strip() and src.poi:
            P["POI"], P["Show POI"] = src.poi, "yes"

    if rec.source == "large_gen":
        gcs = R.GIM_MAP.get(rec.gim)
        if gcs is None:
            res.flag("Unmapped GIM Study Phase", rec, row, rec.gim)
            return P
        if lc == R.CANCELLED:
            res.flag("Cancelled in DB but active in ERCOT Large Gen (not changed)", rec, row,
                     f"GIM '{rec.gim}'; change indicator '{rec.change_ind}'")
            return P
        ia = gcs == R.IA_EXECUTED
        eia_no = row[EIA].strip().lower() != "yes"
        P.update({GCS: gcs, ISO: R.iso_status(rec.gim), GCA: "Yes" if ia else "No"})
        if ia:
            new_lc = lc if lc in R.ADVANCED else R.LATE     # IA shown: no reason to move advanced rows
        elif lc in R.ADVANCED:
            new_lc = R.EARLY if eia_no else R.LATE           # user: downgrade allowed (non-QC)
            res.flag(f"Downgraded {lc} -> {new_lc} (ERCOT shows no IA)", rec, row, rec.gim)
        elif eia_no:
            new_lc = R.EARLY
        else:
            new_lc = lc if lc in (R.EARLY, R.LATE) else R.EARLY
        if lc == R.SUSPENDED:
            res.flag("Reactivated: Suspended in DB, active in ERCOT Large Gen", rec, row,
                     f"-> {new_lc}; change indicator '{rec.change_ind}'")
        if lc == R.LATE and new_lc == R.EARLY:
            res.flag("Downgraded Late-stage -> Early-stage (ERCOT shows no IA)", rec, row, rec.gim)
        P[LC] = new_lc
        if ia and rec.ia_signed and (lc not in R.ADVANCED or not row[IA_DATE].strip()):
            P[IA_DATE] = fmt_date(rec.ia_signed)
        fill_spv_poi(rec)
        dbcap = num(row[CAP])
        big = (rec.capacity is not None and dbcap is not None and
               abs(rec.capacity - dbcap) > max(0.5, R.CAPACITY_FLAG_PCT * abs(dbcap)))
        if big or ("MW Size" in rec.change_ind and rec.capacity is not None):
            res.flag("Capacity differs (not changed)", rec, row,
                     f"DB {row[CAP]} MW vs ERCOT {fmt_num(rec.capacity)} MW; change indicator '{rec.change_ind}'")

    elif rec.source == "commissioning":
        if lc != R.OPER:
            P.update(R.OPERATIONAL_SET)
            if row["Financing type"].strip() not in ("", "Pending finalization"):
                P.pop("Financing type")       # keep researched financing types
            if row["Financing secured"].strip().startswith("Yes"):
                P.pop("Financing secured")
            P[OPR] = fmt_date(rec.event_date)
            if lc in (R.CANCELLED, R.SUSPENDED):
                res.flag("Commissioned per ERCOT but DB shows " + lc, rec, row, "moved to Operational")
        else:
            P.update({GCA: "Yes", EIA: "Yes"})
            if rec.event_date and not same(row[OPR], fmt_date(rec.event_date)):
                res.flag("Operational date differs (not changed)", rec, row,
                         f"DB {row[OPR] or 'blank'} vs ERCOT approval {fmt_date(rec.event_date)}")
        fill_spv_poi(rec.enrich)
        if rec.enrich and rec.enrich.ia_signed and not row[IA_DATE].strip():
            P[IA_DATE] = fmt_date(rec.enrich.ia_signed)

    elif rec.source in ("inactive", "cancellation"):
        target = R.INACTIVE_SET if rec.source == "inactive" else R.CANCELLED_SET
        if rec.source == "inactive" and lc == R.CANCELLED:
            return P                               # Cancelled stays Cancelled
        if lc in R.ADVANCED:
            res.flag(f"Downgraded {lc} -> {target[LC]} ({R.SOURCE_LABEL[rec.source]})", rec, row,
                     f"ERCOT date {fmt_date(rec.event_date)}")
        P.update(target)
    return P


def _val(m, status, est, rep):
    """Date/amount behind a status: estimated column first, reported as fallback."""
    return (m[rep] if m[status].strip() == "Reported" else (m[est] or m[rep])).strip()


def _missing(m, status, est, rep):
    st = m[status].strip()
    return st in ("", R.NR) or (st == "Estimated" and not (m[est].strip() or m[rep].strip()))


def estimate_existing(row: dict, P: dict, run: dt.date, rec: ErcotRec, res: Result,
                      is_qc: bool = False) -> dict:
    m = {**row, **P}
    old_lc, new_lc = row[LC], m[LC]
    changed = old_lc != new_lc
    sector, sub = m["Sector"], m["Sub-sector"]
    cap = num(m[CAP])
    secname = E.sector_comment_name(sector, sub)
    out = {}

    if new_lc in (R.CANCELLED, R.SUSPENDED):
        # statuses/dates are cleared by field_rules; non-QC rows also lose the auto comment blocks
        if not is_qc:
            c = E.strip_auto_sections(row[AC])
            out[AC], out[SAC] = c, yn(c)
        return out
    if new_lc not in (R.EARLY, R.LATE, R.OPER) or not cap or cap <= 0:
        return out

    fc_s, op_s, inv_s = m[FCS].strip(), m[OPS].strip(), m[INVS].strip()
    fc = parse_db_date(_val(m, FCS, FCE, FCR))
    op = parse_db_date(_val(m, OPS, OPE, OPR))
    need_fc = fc_s != "Reported" and (changed or _missing(m, FCS, FCE, FCR))
    need_op = new_lc != R.OPER and op_s != "Reported" and (changed or _missing(m, OPS, OPE, OPR))
    need_inv = (not is_qc) and inv_s != "Reported" and (changed or _missing(m, INVS, INVE, INVR))
    metric = None

    if new_lc == R.OPER:
        op = parse_db_date(m[OPR])
        if need_fc and op:
            fc = E.estimate_fc_from_cod(op, cap, sector)
            out.update({FCS: "Estimated", FCE: fmt_date(fc), FCR: ""})
            fc_s = "Estimated"
    else:
        if need_fc:
            fc = E.estimate_fc_dev(new_lc, cap, run)
            if op_s == "Reported" and op:
                limit = E.add_months_eom(op, -E.cod_min(sector))
                if fc > limit:
                    floor = E.add_months_eom(run, R.FC_MIN_MONTHS[new_lc])
                    fc = max(limit, floor)
                    if limit < floor:
                        res.flag("Reported COD too close to (or before) estimated FC", rec, row,
                                 f"reported COD {fmt_date(op)}; estimated FC {fmt_date(fc)}")
            out.update({FCS: "Estimated", FCE: fmt_date(fc), FCR: ""})
            fc_s = "Estimated"
        if need_op:
            op = E.estimate_cod_from_fc(fc or E.estimate_fc_dev(new_lc, cap, run), cap, sector)
            out.update({OPS: "Estimated", OPE: fmt_date(op), OPR: ""})
            op_s = "Estimated"

    if need_inv:
        inv, metric = E.estimate_investment(sector, cap, new_lc, parse_db_date(m[OPR]), run)
        out.update({INVS: "Estimated", INVE: str(inv), INVR: ""})
        inv_s = "Estimated"

    if is_qc:
        # QC rows: only the "Estimated development timeline" paragraph is touched
        if changed or need_fc or need_op:
            tl_sec = E.timeline_section(fc_s, fc, op_s, op, new_lc, secname) if (fc and op) else None
            c = E.replace_timeline_in_place(row[AC], tl_sec)
            out[AC], out[SAC] = c, yn(c)
        return out

    if changed or need_fc or need_op or need_inv:
        inv_sec = tl_sec = None
        if metric is not None:
            inv_sec = E.investment_section(num(out[INVE]), metric, secname)
        if fc and op:
            tl_sec = E.timeline_section(fc_s, fc, op_s, op, new_lc, secname)
        replace_tl = changed or need_fc or need_op
        c = E.rebuild_comment(row[AC], inv_sec, tl_sec, changed or need_inv, replace_tl)
        out[AC], out[SAC] = c, yn(c)
    return out


# ---------------------------------------------------------------- new rows
def build_new(rec: ErcotRec, lookup: CountyLookup, run: dt.date, res: Result) -> dict:
    r = {c: "" for c in R.NEW_COLUMNS}
    r.update(R.NEW_STATIC)
    r.update({"Project name": rec.name, "Interconnection ID": rec.inr_raw, CAP: fmt_num(rec.capacity),
              "Sector": rec.sector, "Sub-sector": rec.subsector, "Sub-location": rec.county,
              "Solar technology": "PV" if rec.sector == "Solar" else ""})
    geo, in_table, fb = lookup.get(rec.county)
    r.update(geo)
    if not in_table:
        res.flag("County not in SOP lookup table", rec, None,
                 f"{rec.county}: " + ("filled from existing DB rows" if fb else "left blank"))
    elif fb:
        res.flag("County lookup gap filled from DB", rec, None, f"{rec.county}: {', '.join(fb)}")

    src = rec.enrich if rec.source == "commissioning" else rec
    if rec.source in ("large_gen", "commissioning"):
        if src is not None:
            r["POI"], r["SPV"] = src.poi, src.spv
            if src.ia_signed:
                r[IA_DATE] = fmt_date(src.ia_signed)
        r["Show POI"], r["Show SPV"] = yn(r["POI"]), yn(r["SPV"])
    else:
        r["Show POI"] = r["Show SPV"] = "no"

    if rec.source == "large_gen":
        gcs = R.GIM_MAP.get(rec.gim, "")
        if not gcs:
            res.flag("Unmapped GIM Study Phase (new project)", rec, None, rec.gim)
        ia = gcs == R.IA_EXECUTED
        r.update({GCS: gcs, ISO: R.iso_status(rec.gim) if gcs else "", GCA: "Yes" if ia else "No",
                  EIA: "No", LC: R.LATE if ia else R.EARLY, "PPA signed": "No",
                  "Financing secured": "No"})
    elif rec.source == "commissioning":
        r.update(R.OPERATIONAL_SET)
        r[OPR] = fmt_date(rec.event_date)
    else:
        r.update(R.INACTIVE_SET if rec.source == "inactive" else R.CANCELLED_SET)
        r["Power sales arrangement"] = R.PSA_TERMINAL
        if rec.sector == "Battery":
            r["Revenue mechanism"] = R.BATTERY_REVENUE_TERMINAL

    lc, cap = r[LC], rec.capacity
    secname = E.sector_comment_name(rec.sector, rec.subsector)
    if lc in (R.CANCELLED, R.SUSPENDED):
        r[FCS] = r[OPS] = r[INVS] = R.NR
        r[SAC] = "no"
        r.update(FR.apply(r))
        return r

    if lc == R.OPER:
        op = rec.event_date
        fc = E.estimate_fc_from_cod(op, cap, rec.sector)
        r.update({FCS: "Estimated", FCE: fmt_date(fc)})
        op_s = "Reported"
    else:
        fc = E.estimate_fc_dev(lc, cap, run)
        op = E.estimate_cod_from_fc(fc, cap, rec.sector)
        r.update({FCS: "Estimated", FCE: fmt_date(fc), OPS: "Estimated", OPE: fmt_date(op)})
        op_s = "Estimated"
    inv, metric = E.estimate_investment(rec.sector, cap, lc, op, run)
    r.update({INVS: "Estimated", INVE: str(inv)})
    parts = [E.investment_section(inv, metric, secname),
             E.timeline_section("Estimated", fc, op_s, op, lc, secname)]
    r[AC] = "\n\n".join(p for p in parts if p)
    r[SAC] = yn(r[AC])
    r.update(FR.apply(r))
    return r


# ---------------------------------------------------------------- driver
def build_index(db: pd.DataFrame) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i, v in db["Interconnection ID"].items():
        for part in re.split(r"[;,]", v or ""):
            k = norm_inr(part)
            if k and i not in idx.get(k, []):
                idx.setdefault(k, []).append(i)
    return idx


def _process(rec: ErcotRec, row: dict, res: Result, run_date: dt.date, extra: dict | None = None):
    """Existing-row pipeline: status proposal, QC guard, estimates, field rules, change capture."""
    is_qc = row["Lead QC"].strip().lower() == "yes"
    n_flags = len(res.review)
    P = propose(rec, row, res)
    new_lc = P.get(LC, row[LC])
    if is_qc and new_lc != row[LC] and \
            R.LIFECYCLE_RANK.get(new_lc, 0) <= R.LIFECYCLE_RANK.get(row[LC], 0):
        res.review[n_flags:] = [f for f in res.review[n_flags:]
                                if not f["Flag"].startswith("Downgraded")]
        res.flag("QC row: lifecycle downgrade not applied", rec, row,
                 f"ERCOT points to {row[LC]} -> {new_lc}; only SPV/POI blanks filled")
        P = {k: v for k, v in P.items() if k in ("SPV", "Show SPV", "POI", "Show POI")}
    P.update(extra or {})
    P.update(estimate_existing(row, P, run_date, rec, res, is_qc))
    P.update(FR.apply({**row, **P}))
    changes = [(f, row.get(f, ""), v) for f, v in P.items() if not same(row.get(f, ""), v)]
    if not changes:
        return
    summary = "\n".join(f"{f}: {_short(o)} -> {_short(n)}" for f, o, n in changes)
    upd = {**row, **{f: n for f, _, n in changes}, "_changed": {f for f, _, _ in changes}}
    (res.qc if is_qc else res.updates).append(upd)
    res.review.append({"Type": "Change (QC)" if is_qc else "Change", "Flag": "",
                       "Source sheet": R.SOURCE_LABEL[rec.source],
                       "Interconnection ID": upd["Interconnection ID"],
                       "Project ID": row["Project ID"], "Project name": row["Project name"],
                       "Lead QC": row["Lead QC"], "Detail": summary})


def run(db: pd.DataFrame, ercot: dict, run_date: dt.date, county_csv: Path,
        noid: pd.DataFrame | None = None, match_opts: dict | None = None) -> Result:
    res = Result()
    skipped = {"zero_mw": set(), "sync": 0}
    for src in ("large_gen", "commissioning", "inactive", "cancellation"):
        keep = []
        for r in ercot[src]:
            if src == "commissioning" and any(k in r.category for k in R.COMMISSIONING_SKIP):
                skipped["sync"] += 1
                continue
            if r.capacity is None or r.capacity <= 0:        # user: ignore 0 MW projects
                skipped["zero_mw"].add(r.inr)
                continue
            keep.append(r)
        ercot = {**ercot, src: keep}
    recs = unify(ercot)
    idx = build_index(db)
    lookup = CountyLookup(county_csv, db)
    prec = {s: i for i, s in enumerate(R.PRECEDENCE)}
    db_zero = {i for i in db.index if not (num(db.at[i, CAP]) or 0) > 0}

    row_recs: dict[int, list[ErcotRec]] = {}
    new_recs = []
    for inr, rec in recs.items():
        rows = idx.get(inr, [])
        if not rows:
            new_recs.append(rec)
            continue
        if len(rows) > 1:
            res.flag("Interconnection ID on several DB rows (each updated separately)", rec, None,
                     "; ".join(f"{db.at[i, 'Project ID']} ({db.at[i, 'Project name']}, QC {db.at[i, 'Lead QC']})"
                               for i in rows))
        for i in rows:
            if i not in db_zero:
                row_recs.setdefault(i, []).append(rec)

    for i, rl in row_recs.items():
        rl.sort(key=lambda r: prec[r.source])
        rec, row = rl[0], db.loc[i].to_dict()
        if len({r.inr for r in rl}) > 1:
            res.flag("DB row holds several INRs; highest-priority sheet applied", rec, row,
                     ", ".join(f"{r.inr_raw} ({R.SOURCE_LABEL[r.source]})" for r in rl))
        _process(rec, row, res, run_date)

    # new INRs matched (GPT) to DB projects that have no Interconnection ID
    if noid is not None and len(noid):
        applied, decisions, mstats = id_matcher.match(new_recs, noid, **(match_opts or {}))
        res.id_matches = id_matcher.decisions_table(decisions)
        res.match_stats = mstats
        by_inr = {d.inr.upper(): d for d in decisions}
        still_new = []
        for rec in new_recs:
            i = applied.get(rec.inr)
            if i is None:
                still_new.append(rec)
                continue
            row = noid.loc[i].to_dict()
            d = by_inr[rec.inr]
            res.flag(f"Matched to DB project without Interconnection ID ({d.method}, {d.confidence})",
                     rec, row, d.reason)
            _process(rec, row, res, run_date, extra={"Interconnection ID": rec.inr_raw})
        new_recs = still_new

    order = {s: i for i, s in enumerate(["large_gen", "commissioning", "cancellation", "inactive"])}
    for rec in sorted(new_recs, key=lambda r: (order[r.source], r.inr)):
        res.new.append(build_new(rec, lookup, run_date, res))

    # active DB rows whose INR is not in any ERCOT sheet this month
    seen = set(recs) | skipped["zero_mw"]
    active = {R.EARLY, R.LATE, "Ready-to-build", "In construction"}
    for i, row in db.iterrows():
        if i in db_zero:
            continue
        ids = [norm_inr(p) for p in re.split(r"[;,]", row["Interconnection ID"]) if "INR" in p.upper()]
        if row[LC] in active and ids and not any(k in seen for k in ids):
            res.flag("Active in DB but not in any ERCOT sheet this month", None, row.to_dict(),
                     f"DB lifecycle {row[LC]}")

    res.stats = {
        "ERCOT INRs in scope": len(recs),
        "Matched to DB": len(recs) - len(new_recs),
        "New projects": len(res.new),
        "Existing rows updated (non-QC)": len(res.updates),
        "Existing rows updated (QC)": len(res.qc),
        "Flags": sum(1 for r in res.review if r["Type"] == "Flag"),
        "ERCOT 0 MW projects ignored": len(skipped["zero_mw"]),
        "Commissioning 'Synchronization Approved' rows ignored": skipped["sync"],
        "DB rows with 0 MW ignored": len(db_zero),
        **res.match_stats,
    }
    return res


def _short(v, n=90):
    s = (v or "").replace("\r\n", " / ").replace("\n", " / ")
    return (s[: n - 3] + "...") if len(s) > n else (s or "blank")
