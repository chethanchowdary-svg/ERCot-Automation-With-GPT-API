import pandas as pd

from ercot_qb import ercot_source as ES
from ercot_qb import quickbase_source as QS
from ercot_qb import rules as R


class Resp:
    def __init__(self, js=None, status=200, content=b""):
        self._js, self.status_code, self.content, self.text, self.headers = js, status, content, "", {}

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


def test_list_gis_reports(monkeypatch):
    js = {"ListDocsByRptTypeRes": {"DocumentList": [
        {"Document": {"DocID": "1", "FriendlyName": "GIS_Report_July2026", "PublishDate": "2026-08-03T15:39:50-05:00"}},
        {"Document": {"DocID": "2", "FriendlyName": "Co-located_Battery_Identification_Report_August_2026",
                      "PublishDate": "2026-09-09T10:23:33-05:00"}},
        {"Document": {"DocID": "3", "FriendlyName": "GIS_Report_August2026", "PublishDate": "2026-09-01T14:38:05-05:00"}},
    ]}}

    class S:
        headers, proxies = {}, {}

        def get(self, url, timeout):
            return Resp(js)
    monkeypatch.setattr(ES, "_session", lambda proxy: S())
    reps = ES.list_gis_reports()
    assert [r["name"] for r in reps] == ["GIS_Report_August2026", "GIS_Report_July2026"]


def test_quickbase_api(monkeypatch):
    fields = [{"id": i + 3, "label": c, "type": "text"} for i, c in enumerate(R.DB_COLUMNS)]
    fid = {f["label"]: str(f["id"]) for f in fields}
    for f in fields:
        if f["label"] in ("Lead QC", "Show POI"):
            f["type"] = "checkbox"
        if f["label"] == "Interconnection signed date":
            f["type"] = "date"
    rec = {fid[c]: {"value": ""} for c in R.DB_COLUMNS}
    rec[fid["Lead QC"]] = {"value": True}
    rec[fid["Interconnection signed date"]] = {"value": "2023-11-02"}
    rec[fid["Financing type"]] = {"value": ["Debt", "Equity"]}
    calls = []

    def post(url, params, headers, timeout):
        calls.append(params["skip"])
        data = [rec] if params["skip"] == 0 else []
        return Resp({"data": data, "fields": fields,
                     "metadata": {"totalRecords": 1, "numRecords": len(data)}})
    import requests
    monkeypatch.setattr(requests, "post", post)
    df = QS.load_api("x.quickbase.com", "tok", "t", "1")
    assert df.loc[0, "Lead QC"] == "yes"
    assert df.loc[0, "Interconnection signed date"] == "11-02-2023"
    assert df.loc[0, "Financing type"] == "Debt;Equity"
