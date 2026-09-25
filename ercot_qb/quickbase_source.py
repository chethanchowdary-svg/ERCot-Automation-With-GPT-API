"""Load the Quickbase ERCOT projects table."""
from __future__ import annotations

import io

import pandas as pd

from . import rules as R


def load_csv(path) -> pd.DataFrame:
    """Quickbase exports mix UTF-8 and cp1252 bytes; decode line by line."""
    raw = open(path, "rb").read()
    lines = []
    for line in raw.split(b"\n"):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(line.decode("cp1252", errors="replace"))
    df = pd.read_csv(io.StringIO("\n".join(lines)), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in R.DB_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Quickbase file is missing columns: {missing}")
    return df


# ---------------------------------------------------------------- API
API = "https://api.quickbase.com/v1"
PAGE = 1000


def _to_text(value, ftype: str) -> str:
    """Render a Quickbase API value the way the CSV export shows it."""
    if value is None:
        return ""
    if ftype == "checkbox" or isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ";".join(_to_text(v, "") for v in value if v not in (None, ""))
    if isinstance(value, dict):          # user / file attachment fields
        return str(value.get("name") or value.get("email") or value.get("url") or "")
    if ftype in ("date", "timestamp") and value:
        d = pd.to_datetime(str(value), errors="coerce")
        return "" if pd.isna(d) else d.strftime(R.DATE_FMT)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def load_api(realm: str, token: str, table_id: str, report_id: str) -> pd.DataFrame:
    """Run the saved Quickbase report and return it with the export's column names/format."""
    import requests
    if not token:
        raise ValueError("QB_USER_TOKEN is empty. Add it as a GitHub secret or env variable.")
    headers = {"QB-Realm-Hostname": realm, "Authorization": f"QB-USER-TOKEN {token}",
               "User-Agent": "enerdatics-ercot-automation", "Content-Type": "application/json"}
    rows, fields, skip, total = [], None, 0, None
    while total is None or skip < total:
        r = requests.post(f"{API}/reports/{report_id}/run",
                          params={"tableId": table_id, "skip": skip, "top": PAGE},
                          headers=headers, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Quickbase API {r.status_code}: {r.text[:500]}")
        js = r.json()
        fields = fields or {str(f["id"]): (f["label"].strip(), f.get("type", "")) for f in js["fields"]}
        for rec in js["data"]:
            rows.append({fields[k][0]: _to_text(v.get("value"), fields[k][1])
                         for k, v in rec.items() if k in fields})
        meta = js.get("metadata", {})
        total = meta.get("totalRecords", len(rows))
        got = meta.get("numRecords", len(js["data"]))
        if got == 0:
            break
        skip += got
    df = pd.DataFrame(rows, columns=[lbl for lbl, _ in fields.values()] if fields else None).fillna("")
    missing = [c for c in R.DB_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Quickbase report {report_id} is missing columns: {missing}")
    return df
