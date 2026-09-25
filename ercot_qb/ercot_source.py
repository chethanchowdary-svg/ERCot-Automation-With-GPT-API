"""Read the ERCOT monthly GIS report workbook into clean records."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import rules as R

INR_RE = re.compile(r"^\d{2}INR\d+[A-Za-z]?$")


def norm_inr(v) -> str:
    return str(v).strip().upper() if v is not None else ""


def to_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        d = pd.to_datetime(str(v), errors="coerce")
        return None if pd.isna(d) else d.date()
    except Exception:
        return None


def to_float(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return None if pd.isna(f) else f
    except Exception:
        return None


def clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


@dataclass
class ErcotRec:
    inr: str                  # normalised (upper) for matching
    source: str               # large_gen | commissioning | inactive | cancellation
    name: str
    county: str
    capacity: float | None
    sector: str
    subsector: str
    gim: str = ""
    spv: str = ""
    poi: str = ""
    ia_signed: dt.date | None = None
    event_date: dt.date | None = None   # approval / inactive / cancel date
    category: str = ""                  # commissioning category
    change_ind: str = ""
    enrich: "ErcotRec | None" = None    # Large Gen twin (for commissioning rows)
    inr_raw: str = ""         # as published by ERCOT (used in outputs)


def _read_block(path, sheet, cols=None) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    is_hdr = raw.apply(lambda r: r.astype(str).str.strip().eq("INR").any(), axis=1)
    hr = raw.index[is_hdr][0]
    inr_col = [i for i, v in enumerate(raw.iloc[hr]) if str(v).strip() == "INR"][0]
    body = raw.iloc[hr + 1:].copy()
    names = cols or [clean(v) for v in raw.iloc[hr]]
    names = list(names)[: body.shape[1]] + [f"_x{i}" for i in range(body.shape[1] - len(names))]
    body.columns = names
    keep = body.iloc[:, inr_col].astype(str).str.strip().str.match(INR_RE)
    return body[keep].reset_index(drop=True)


def load_ercot(path) -> dict:
    """Return {'large_gen': [...], 'small_gen': DataFrame, 'commissioning': [...], ...}."""
    out = {}

    lg = _read_block(path, R.SHEET_LARGE, R.LARGE_GEN_COLS)
    lg = lg[lg["Technology"].astype(str).str.strip().isin(R.TECH_MAP)]
    recs = []
    for _, r in lg.iterrows():
        sec, sub = R.TECH_MAP[str(r["Technology"]).strip()]
        recs.append(ErcotRec(
            inr=norm_inr(r["INR"]), inr_raw=clean(r["INR"]), source="large_gen", name=clean(r["Project Name"]),
            county=clean(r["County"]), capacity=to_float(r["Capacity (MW)"]), sector=sec,
            subsector=sub, gim=clean(r["GIM Study Phase"]), spv=clean(r["Interconnecting Entity"]),
            poi=clean(r["POI Location"]), ia_signed=to_date(r["IA Signed"]),
            change_ind=clean(r["Change indicators"]),
        ))
    out["large_gen"] = recs

    sg = _read_block(path, R.SHEET_SMALL)
    out["small_gen"] = sg[sg["Technology"].astype(str).str.strip().isin(R.TECH_MAP)]

    def fuel_sheet(sheet, source, date_col):
        df = _read_block(path, sheet)
        df = df[df["Fuel"].astype(str).str.strip().isin(R.FUEL_MAP)]
        items = []
        for _, r in df.iterrows():
            sec, sub = R.FUEL_MAP[str(r["Fuel"]).strip()]
            items.append(ErcotRec(
                inr=norm_inr(r["INR"]), inr_raw=clean(r["INR"]), source=source, name=clean(r["Project Name"]),
                county=clean(r["County"]), capacity=to_float(r["MW **"]), sector=sec, subsector=sub,
                event_date=to_date(r[date_col]),
                category=clean(r.get("Commissioning Category", "")),
            ))
        return items

    out["commissioning"] = fuel_sheet(R.SHEET_COMM, "commissioning", "Approval Date *")
    out["inactive"] = fuel_sheet(R.SHEET_INACTIVE, "inactive", "Inactive Date")
    out["cancellation"] = fuel_sheet(R.SHEET_CANCEL, "cancellation", "Cancel Date")
    return out


def unify(ercot: dict) -> dict[str, ErcotRec]:
    """One record per INR using sheet precedence; commissioning rows keep their Large Gen twin."""
    lg_by_inr = {r.inr: r for r in ercot["large_gen"]}
    merged: dict[str, ErcotRec] = {}
    for src in reversed(R.PRECEDENCE):          # lowest first, higher overwrites
        for r in ercot[src]:
            if src != "large_gen" and r.inr in lg_by_inr:
                r.enrich = lg_by_inr[r.inr]
            merged[r.inr] = r
    return merged


# ---------------------------------------------------------------- download
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={rtid}"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}"
PRODUCT_PAGE = "https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": PRODUCT_PAGE,
}


def _session(proxy: str | None):
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def list_gis_reports(report_type_id: int = R.ERCOT_REPORT_TYPE_ID, proxy: str | None = None) -> list[dict]:
    """All 'GIS_Report_<Month><Year>' files on the EMIL page, newest first."""
    s = _session(proxy)
    r = s.get(LIST_URL.format(rtid=report_type_id), timeout=60)
    r.raise_for_status()
    try:
        docs = r.json()["ListDocsByRptTypeRes"]["DocumentList"]
    except Exception as e:  # geo-block / bot page returns HTML
        raise RuntimeError(
            "ERCOT did not return the report list (likely blocked outside the US). "
            "Use a US runner/proxy or pass --ercot-file / --ercot-url.") from e
    if isinstance(docs, dict):
        docs = [docs]
    out = []
    for d in docs:
        doc = d.get("Document", d)
        name = str(doc.get("FriendlyName") or doc.get("ConstructedName") or "")
        if not name.lower().startswith(R.ERCOT_NAME_PREFIX.lower()):
            continue           # skips Co-located_Battery_Identification_Report_*
        out.append({"name": name, "doc_id": str(doc["DocID"]),
                    "published": pd.Timestamp(doc.get("PublishDate")),
                    "constructed": doc.get("ConstructedName", "")})
    return sorted(out, key=lambda x: x["published"], reverse=True)


def download(url: str, dest: Path, proxy: str | None = None) -> Path:
    s = _session(proxy)
    r = s.get(url, timeout=180)
    r.raise_for_status()
    if not r.content.startswith(b"PK"):   # xlsx is a zip
        raise RuntimeError(f"Download from {url} is not an xlsx (got {r.headers.get('content-type')}).")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def fetch_gis_report(out_dir: Path, name: str | None = None, proxy: str | None = None) -> tuple[Path, dict]:
    """Download the latest GIS report, or the one whose friendly name matches `name`."""
    reports = list_gis_reports(proxy=proxy)
    if not reports:
        raise RuntimeError("No GIS_Report_* files found on the ERCOT page.")
    if name:
        match = [r for r in reports if r["name"].lower() == name.strip().lower()]
        if not match:
            raise RuntimeError(f"{name} not found. Available: {[r['name'] for r in reports[:12]]}")
        rep = match[0]
    else:
        rep = reports[0]
    path = download(DOWNLOAD_URL.format(doc_id=rep["doc_id"]), out_dir / f"{rep['name']}.xlsx", proxy)
    return path, rep
