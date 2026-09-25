"""Match 'new' ERCOT INRs to Quickbase ERCOT projects that have no Interconnection ID.

Matching priority (both shortlist and GPT):
  1. County (Sub-location)  -- 40 pts -- strongest signal; mismatch almost always disqualifies
  2. Sector                 -- pre-filtered (same sector only)
  3. SPV / Interconnecting entity -- 25 pts
  4. Project name           -- 20 pts  (distinctive words only, ignoring Solar/Wind/BESS/LLC etc.)
  5. Capacity (MW)          -- 15 pts  (ratio-based; modest differences are fine)

Total shortlist score max = 100.
Admission gate: county must match OR (name >= 80 OR spv >= 85).
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
import requests
from rapidfuzz import fuzz

from .ercot_source import ErcotRec

SECTORS = ("Wind", "Solar", "Battery")
STOP = set("""solar wind storage bess battery energy project projects farm center centre facility
power generation station llc lp inc renewable renewables ess slf pv the and of co company hybrid
plant park windpower""".split())
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
DIFFERENT_ASSET = re.compile(r"\b(repower\w*|uprate|expansion|alt|alternat\w*|addition|augment\w*)\b", re.I)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Scoring weights (must sum to 100)
W_COUNTY = 40
W_SPV    = 25
W_NAME   = 20
W_CAP    = 15

SYSTEM_PROMPT = """You reconcile ERCOT interconnection queue entries with a renewable energy project database.
You get ONE ERCOT entry and up to five database candidates that have no interconnection ID yet.
Decide whether exactly one candidate is the SAME physical project as the ERCOT entry.

Evaluate signals in this STRICT priority order:
1. County (Sub-location) -- STRONGEST signal. A county mismatch is almost always a disqualifier.
   Only override if an SPV or POI name makes it absolutely clear it is the same asset.
2. Sector -- must match (Solar / Wind / Battery); already pre-filtered but confirm.
3. SPV / Interconnecting entity -- a name match here is strong supporting evidence.
4. Project name -- compare the distinctive part only, ignoring generic words such as Solar, Wind,
   BESS, Storage, Energy, Project, Farm, LLC, Inc, LP.
5. Capacity (MW) -- supporting signal only; modest differences (< 20%) are acceptable.

NOT a match when:
- Counties differ (unless overridden by clear SPV/POI evidence).
- ERCOT entry is a repower, uprate, expansion, alternative POI, or a different phase/number
  (II vs I, 2 vs 1, Phase B vs Phase A) of the candidate.
- An operational database project paired with a fresh queue entry is likely a repower/expansion --
  treat as no match unless SPV or POI explicitly confirms it is the same physical asset.

Reply with JSON only:
{"match_project_id": "<Project ID of the candidate or null>",
 "confidence": "high" | "medium" | "low",
 "reason": "<one short sentence>"}
Use "high" only when county agrees AND at least one of (SPV or distinctive project name) clearly
agrees AND nothing suggests a different phase or asset."""


# ---------------------------------------------------------------- helpers
def norm_name(s: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(w for w in t.split() if w not in STOP)


def phase_numbers(s: str) -> set[int]:
    out = set()
    for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split():
        if w.isdigit() and int(w) < 20:
            out.add(int(w))
        elif w in ROMAN:
            out.add(ROMAN[w])
    return out


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("$", ""))
    except ValueError:
        return None


@dataclass
class Decision:
    inr: str
    ercot_sheet: str
    ercot_name: str
    sector: str
    ercot_mw: float | None
    county: str
    project_id: str = ""
    db_name: str = ""
    db_mw: str = ""
    db_county: str = ""
    db_lifecycle: str = ""
    lead_qc: str = ""
    confidence: str = ""
    reason: str = ""
    method: str = ""          # gpt | manual | none
    candidates: int = 0
    applied: bool = False


# ---------------------------------------------------------------- shortlist
def prepare_noid(noid: pd.DataFrame) -> pd.DataFrame:
    d = noid[(noid["Interconnection ID"].str.strip() == "") &
             (noid["Sector"].isin(SECTORS)) &
             (noid["Power market"].str.contains("ERCOT", case=False, na=False))].copy()
    d["_n"] = d["Project name"].map(norm_name)
    d["_s"] = d["SPV"].map(norm_name)
    d["_cap"] = d["Power Capacity (MW)"].map(_num)
    d["_cty"] = d["Sub-location"].str.strip().str.lower()
    return d


def shortlist(rec: ErcotRec, noid: pd.DataFrame, top: int = 5,
              excluded: set[str] = frozenset()) -> list[tuple[float, int]]:
    """Rank same-sector candidates by county > SPV > name > capacity.

    Priority weights: county 40, SPV 25, name 20, capacity 15 (total max 100).
    Admission: county matches OR (name >= 80 OR spv >= 85).
    """
    c = noid[(noid["Sector"] == rec.sector) & (~noid["Project ID"].isin(excluded))]
    rn, rs = norm_name(rec.name), norm_name(rec.spv)
    ercot_cty = rec.county.strip().lower() if rec.county else ""
    out = []
    for i, d in c.iterrows():
        same_cty = bool(ercot_cty) and d["_cty"] == ercot_cty
        ns = fuzz.token_set_ratio(rn, d["_n"]) if rn and d["_n"] else 0
        ss = max(fuzz.token_set_ratio(rs, d["_s"]) if rs and d["_s"] else 0,
                 fuzz.token_set_ratio(rs, d["_n"]) if rs and d["_n"] else 0)
        # admission: county OR strong name/SPV signal
        if not (same_cty or ns >= 80 or ss >= 85):
            continue
        cap = d["_cap"]
        capr = min(rec.capacity, cap) / max(rec.capacity, cap) if cap and rec.capacity else 0
        score = (W_COUNTY if same_cty else 0) + W_SPV * ss / 100 + W_NAME * ns / 100 + W_CAP * capr
        if score >= 15:
            out.append((score, i))
    out.sort(reverse=True)
    return out[:top]


# ---------------------------------------------------------------- GPT
def _payload(rec: ErcotRec, noid: pd.DataFrame, cands) -> dict:
    return {
        "ercot_entry": {
            "INR": rec.inr_raw, "name": rec.name, "sector": rec.sector, "MW": rec.capacity,
            "county": rec.county, "interconnecting_entity": rec.spv, "poi": rec.poi,
            "ercot_sheet": rec.source, "gim_phase": rec.gim, "commissioning_category": rec.category,
            "change_indicator": rec.change_ind,
        },
        "candidates": [{
            "project_id": noid.at[i, "Project ID"],
            "name": noid.at[i, "Project name"],
            "MW": noid.at[i, "Power Capacity (MW)"],
            "county": noid.at[i, "Sub-location"],
            "spv": noid.at[i, "SPV"],
            "poi": noid.at[i, "POI"],
            "owner": noid.at[i, "Ownership"],
            "lifecycle": noid.at[i, "Project life cycle"],
            "operational_date": (noid.at[i, "Operational date (reported)"] or
                                 noid.at[i, "Operational date (estimated)"]),
        } for _, i in cands],
    }


def ask_gpt(payload: dict, api_key: str, model: str, retries: int = 4) -> dict:
    body = {"model": model, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(payload, default=str)}]}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(retries):
        r = requests.post(OPENAI_URL, headers=headers, json=body, timeout=120)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI API {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(re.sub(r"^```(json)?|```$", "", content.strip()).strip())
    raise RuntimeError("OpenAI API kept failing after retries")


# ---------------------------------------------------------------- overrides
def load_overrides(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Interconnection ID -> [(Project ID, 'match'|'reject')]; blank Project ID + reject = never match."""
    out: dict[str, list[tuple[str, str]]] = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            inr = (r.get("Interconnection ID") or "").strip().upper()
            dec = (r.get("Decision") or "").strip().lower()
            if inr and dec in ("match", "reject"):
                out.setdefault(inr, []).append(((r.get("Project ID") or "").strip(), dec))
    return out


# ---------------------------------------------------------------- driver
def match(new_recs: list[ErcotRec], noid_raw: pd.DataFrame, overrides_path: Path | None = None,
          api_key: str | None = None, model: str | None = None, max_calls: int = 600,
          ask=ask_gpt) -> tuple[dict[str, int], list[Decision], dict]:
    """Return ({INR: noid row index} for applied matches, all decisions, stats)."""
    noid = prepare_noid(noid_raw)
    overrides = load_overrides(overrides_path)
    api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
    model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    stats = {"no-ID DB projects considered": len(noid), "GPT calls": 0}
    proposals: list[tuple[float, Decision, int]] = []
    decisions: list[Decision] = []

    for rec in new_recs:
        if rec.sector not in SECTORS:
            continue
        dec = Decision(rec.inr_raw, rec.source, rec.name, rec.sector, rec.capacity, rec.county)
        ov = overrides.get(rec.inr, [])
        forced = [pid for pid, d in ov if d == "match" and pid]
        rejected = {pid for pid, d in ov if d == "reject"}
        if "" in rejected:
            continue
        if forced:
            hit = noid.index[noid["Project ID"] == forced[0]]
            if len(hit):
                dec.method, dec.confidence, dec.reason = "manual", "high", "Manual override"
                proposals.append((1000.0, dec, hit[0]))
                decisions.append(dec)
            continue
        cands = shortlist(rec, noid, excluded=rejected)
        if not cands:
            continue
        dec.candidates = len(cands)
        if not api_key:
            dec.method = "none"
            dec.reason = "OPENAI_API_KEY not set; candidates found but not checked"
            _fill_db(dec, noid, cands[0][1])
            decisions.append(dec)
            continue
        if stats["GPT calls"] >= max_calls:
            dec.method = "none"
            dec.reason = f"GPT call limit ({max_calls}) reached"
            decisions.append(dec)
            continue
        try:
            ans = ask(_payload(rec, noid, cands), api_key, model)
            stats["GPT calls"] += 1
        except Exception as e:
            dec.method = "none"
            dec.reason = f"GPT error: {e}"[:300]
            decisions.append(dec)
            continue
        dec.method = "gpt"
        pid = str(ans.get("match_project_id") or "").strip()
        dec.confidence = str(ans.get("confidence") or "low").lower()
        dec.reason = str(ans.get("reason") or "")
        valid = {noid.at[i, "Project ID"]: i for _, i in cands}
        if pid and pid in valid:
            i = valid[pid]
            _fill_db(dec, noid, i)
            guard = _guard(rec.name, noid.at[i, "Project name"])
            if guard and dec.confidence == "high":
                dec.confidence = "medium"
                dec.reason += f" [capped: {guard}]"
            score = next(s for s, j in cands if j == i)
            proposals.append((score, dec, i))
        decisions.append(dec)

    # one INR per DB project: keep the highest-scoring high-confidence claim
    applied: dict[str, int] = {}
    taken: set[int] = set()
    for score, dec, i in sorted(proposals, key=lambda x: -x[0]):
        if dec.confidence != "high":
            continue
        if i in taken:
            dec.reason += " [not applied: DB project already matched to another INR]"
            continue
        taken.add(i)
        applied[dec.inr.upper()] = i
        dec.applied = True

    stats.update({
        "New projects with candidates": sum(1 for d in decisions if d.candidates or d.method == "manual"),
        "Matches applied (moved to existing)": len(applied),
        "Possible matches for review (medium)": sum(
            1 for d in decisions if d.project_id and not d.applied and d.confidence == "medium"),
        "GPT model": model if api_key else "not used (no OPENAI_API_KEY)",
    })
    return applied, decisions, stats


def _fill_db(dec: Decision, noid: pd.DataFrame, i: int):
    dec.project_id = noid.at[i, "Project ID"]
    dec.db_name    = noid.at[i, "Project name"]
    dec.db_mw      = noid.at[i, "Power Capacity (MW)"]
    dec.db_county  = noid.at[i, "Sub-location"]
    dec.db_lifecycle = noid.at[i, "Project life cycle"]
    dec.lead_qc    = noid.at[i, "Lead QC"]


def _guard(ercot_name: str, db_name: str) -> str:
    if DIFFERENT_ASSET.search(ercot_name or ""):
        return "ERCOT name suggests repower/uprate/expansion/alternative POI"
    a, b = phase_numbers(ercot_name), phase_numbers(db_name)
    if a and b and a != b:
        return "phase/number differs"
    return ""


def decisions_table(decisions: list[Decision]) -> list[dict]:
    cols = {
        "inr":        "Interconnection ID",
        "ercot_sheet":"ERCOT sheet",
        "ercot_name": "ERCOT name",
        "sector":     "Sector",
        "ercot_mw":   "ERCOT MW",
        "county":     "ERCOT county",
        "project_id": "Matched Project ID",
        "db_name":    "DB project name",
        "db_mw":      "DB MW",
        "db_county":  "DB county",
        "db_lifecycle":"DB lifecycle",
        "lead_qc":    "Lead QC",
        "confidence": "Confidence",
        "reason":     "Reason",
        "method":     "Method",
        "candidates": "Candidates",
        "applied":    "Applied",
    }
    rows = []
    for d in sorted(decisions, key=lambda d: (not d.applied, d.confidence != "medium", d.inr)):
        x = asdict(d)
        x["applied"] = "Yes" if d.applied else "No"
        rows.append({cols[k]: v for k, v in x.items()})
    return rows
