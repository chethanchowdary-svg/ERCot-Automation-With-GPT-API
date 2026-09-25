"""Command-line entry point.

Examples
  python -m ercot_qb.main                                  # latest ERCOT report + Quickbase API
  python -m ercot_qb.main --ercot-name GIS_Report_July2026
  python -m ercot_qb.main --ercot-file GIS.xlsx --qb-csv Projects.csv   # fully offline
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

from . import engine, writer
from . import ercot_source as ES
from . import quickbase_source as QS

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ERCOT GIS report -> Quickbase upload sheets")
    p.add_argument("--ercot-file", help="Local GIS report xlsx (skips download)")
    p.add_argument("--ercot-url", help="Direct xlsx link copied from the ERCOT page")
    p.add_argument("--ercot-name", help="Friendly name to pick, e.g. GIS_Report_August2026 (default: latest)")
    p.add_argument("--qb-csv", help="Quickbase CSV export (skips the API)")
    p.add_argument("--qb-noid-csv", help="Quickbase CSV of ERCOT projects WITHOUT Interconnection ID")
    p.add_argument("--no-id-match", action="store_true", help="Skip the GPT matching step")
    p.add_argument("--run-date", help="YYYY-MM-DD used as 'present date' for estimates (default: today)")
    p.add_argument("--out-dir", default="outputs")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    proxy = os.getenv("ERCOT_PROXY") or None
    run_date = dt.date.fromisoformat(a.run_date) if a.run_date else dt.date.today()

    # 1. ERCOT
    meta = {}
    if a.ercot_file:
        ercot_path = Path(a.ercot_file)
        meta = {"name": ercot_path.stem, "published": "local file"}
    elif a.ercot_url:
        ercot_path = ES.download(a.ercot_url, out / "ercot_input.xlsx", proxy)
        meta = {"name": "from --ercot-url", "published": ""}
    else:
        ercot_path, meta = ES.fetch_gis_report(out, a.ercot_name or None, proxy)
    print(f"ERCOT report: {meta.get('name')} ({meta.get('published')}) -> {ercot_path}")
    ercot = ES.load_ercot(ercot_path)

    # 2. Quickbase
    if a.qb_csv:
        db = QS.load_csv(a.qb_csv)
        qb_src = f"CSV {a.qb_csv}"
    else:
        db = QS.load_api(os.getenv("QB_REALM", "enerdatics.quickbase.com"), os.getenv("QB_USER_TOKEN", ""),
                         os.getenv("QB_TABLE_ID", "bs8th2g3g"), os.getenv("QB_REPORT_ID", "1000697"))
        qb_src = f"API report {os.getenv('QB_REPORT_ID', '1000697')}"
        db.to_csv(out / "quickbase_snapshot.csv", index=False)
    print(f"Quickbase rows: {len(db)} ({qb_src})")

    noid = None
    if not a.no_id_match:
        if a.qb_noid_csv:
            noid = QS.load_csv(a.qb_noid_csv)
        elif os.getenv("QB_NOID_REPORT_ID"):
            noid = QS.load_api(os.getenv("QB_REALM", "enerdatics.quickbase.com"), os.getenv("QB_USER_TOKEN", ""),
                               os.getenv("QB_TABLE_ID", "bs8th2g3g"), os.getenv("QB_NOID_REPORT_ID"))
            noid.to_csv(out / "quickbase_noid_snapshot.csv", index=False)
        if noid is not None:
            print(f"Quickbase projects without Interconnection ID: {len(noid)}")
        else:
            print("No-ID report not configured (QB_NOID_REPORT_ID / --qb-noid-csv): matching skipped")
    match_opts = {"overrides_path": ROOT / "data" / "id_match_overrides.csv",
                  "max_calls": int(os.getenv("GPT_MAX_CALLS", "600"))}

    # 3. Compare + write
    res = engine.run(db, ercot, run_date, ROOT / "data" / "county_lookup.csv", noid, match_opts)
    res.stats.update({
        "Small Gen WT/PV/BA rows (not processed)": len(ercot["small_gen"]),
        "ERCOT report": f"{meta.get('name')} (published {meta.get('published')})",
        "Quickbase source": f"{qb_src}, {len(db)} rows",
        "Run date used for estimates": run_date.isoformat(),
    })
    m = re.search(r"GIS_Report_([A-Za-z]+\d{4})", str(meta.get("name", "")), re.I)
    tag = m.group(1) if m else "ERCOT"
    path = out / f"ERCOT_QB_Update_{tag}_{run_date:%Y%m%d}.xlsx"
    writer.write(res, path)
    for k, v in res.stats.items():
        print(f"  {k}: {v}")
    print(f"Output: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
