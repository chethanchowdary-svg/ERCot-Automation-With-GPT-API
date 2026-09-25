# ERCOT GIS Report to Quickbase (quarterly)

Downloads the latest ERCOT **GIS_Report_<Month><Year>** (EMIL PG7-200-ER), pulls the ERCOT projects
table from Quickbase, applies the Enerdatics ERCOT rules and produces one workbook:

| Tab | What it is |
|---|---|
| Existing Update (Non-QC) | Quickbase upload sheet, changed rows only, changed cells in light yellow |
| New Projects | Upload sheet for INRs not in Quickbase (Project ID blank) |
| Existing Update (QC) | Upload sheet for Lead QC = yes rows (lifecycle only moves up, only the timeline paragraph of comments is touched) |
| Change Log & Flags | Field-by-field change log plus review flags |
| ID Matches (GPT) | Every new INR checked against Quickbase ERCOT projects with no Interconnection ID |
| Run Summary | Counts, ERCOT file used, Quickbase source, run date |

The run also saves the downloaded ERCOT xlsx and a `quickbase_snapshot.csv` for audit.

## Setup (once)

1. Create a **private** GitHub repo and push this folder.
2. Settings > Secrets and variables > Actions > **Secrets** > add `QB_USER_TOKEN` (your Quickbase user token).
   Never put the token in code.
3. Optional **Variables** (defaults already built in): `QB_REALM` = enerdatics.quickbase.com,
   `QB_TABLE_ID` = bs8th2g3g, `QB_REPORT_ID` = 1000697.
4. For ID matching: secret `OPENAI_API_KEY`, variable `QB_NOID_REPORT_ID` (report of ERCOT projects
   with blank Interconnection ID, same 44 columns), optional variables `OPENAI_MODEL`
   (default gpt-4.1-mini) and `GPT_MAX_CALLS` (default 600).
5. Actions tab > "ERCOT quarterly Quickbase update" > **Run workflow** to test.
   Outputs appear under the run as an artifact (kept 90 days).

Schedule: 06:00 UTC on 5 Jan / 5 Apr / 5 Jul / 5 Oct. Change the `cron` line in
`.github/workflows/ercot_quarterly.yml` to move it.

## ERCOT access

ERCOT blocks non-US traffic (hence your VPN). GitHub-hosted runners run in US data centres, so the
download normally works. If a run fails with "ERCOT did not return the report list", use any of:

- **Proxy:** add secret `ERCOT_PROXY` = `http://user:pass@us-proxy-host:port`.
- **Manual file:** download the xlsx on VPN, commit it to `inputs/`, run the workflow with
  `ercot_file = inputs/GIS_Report_August2026.xlsx`.
- **Self-hosted runner** on a US machine (change `runs-on`).

## Run locally

```bash
pip install -r requirements.txt
export QB_USER_TOKEN=xxxx                        # PowerShell: $env:QB_USER_TOKEN="xxxx"
python -m ercot_qb.main                          # latest ERCOT file + Quickbase API (needs VPN)
python -m ercot_qb.main --ercot-name GIS_Report_July2026
python -m ercot_qb.main --ercot-file GIS.xlsx --qb-csv Projects.csv --run-date 2026-09-23   # offline
```

## Matching new INRs to projects without an Interconnection ID

1. For each new INR, up to 5 Quickbase projects with no ID are shortlisted locally: same sector, and
   same county or a strong name / SPV match, ranked by name, SPV, county and MW. No shortlist = no API call.
2. GPT (`OPENAI_MODEL`) picks one candidate or none, with confidence and a one-line reason.
3. Only **high** confidence is applied. Repower / uprate / expansion / alternative POI wording, or a
   different phase number (II vs I), caps it at medium. One Quickbase project can take one INR.
4. Applied matches leave New Projects: the Quickbase row gets the Interconnection ID and goes through the
   normal existing-row rules (Non-QC or QC sheet). Medium matches are listed for review only.
5. `data/id_match_overrides.csv` fixes decisions permanently (checked before GPT):
   `Interconnection ID,Project ID,Decision` with Decision `match` or `reject`
   (reject with blank Project ID = never match that INR).

Offline: `python -m ercot_qb.main --ercot-file GIS.xlsx --qb-csv Projects.csv --qb-noid-csv NoID.csv`
(set `OPENAI_API_KEY` first; without it the tab lists candidates but nothing is applied).
Skip the step with `--no-id-match`.

## Where the rules live

| File | Contents |
|---|---|
| `ercot_qb/rules.py` | GIM phase map, sector maps, lifecycle sets, estimation constants, investment tiers |
| `ercot_qb/field_rules.py` | PPA / power sales / revenue mechanism / financing type / status-column rules |
| `ercot_qb/estimates.py` | FC, COD, investment maths and analyst-comment text |
| `ercot_qb/engine.py` | Matching (INR = Interconnection ID), existing vs new, QC handling, flags |
| `ercot_qb/id_matcher.py` | Shortlist + GPT prompt + guards for projects without an Interconnection ID |
| `data/county_lookup.csv` | Sub-location to utility / NERC / weather zone table from the SOP |

## Rules summary

- **Scope:** Large Gen rows with Technology WT / PV / BA; Commissioning, Inactive and Cancellation rows
  with Fuel Wind / Solar / Battery Storage. 0 MW projects ignored. Commissioning rows with
  "Synchronization Approved" ignored. Small Gen not processed.
- **Sheet priority per INR:** Cancellation > Commissioning > Inactive > Large Gen.
- **GIM phase:** Grid connection status per the SOP table; ISO field = "ERCOT - " + GIM phase.
  IA executed > Grid connection approved Yes > Late-stage; no IA and EIA No > Early-stage.
- **Commissioning:** Operational, GCA/EIA Yes, Operational date (reported) = Approval Date,
  Financing Yes - Complete / Debt;Equity (researched financing types kept), PPA Yes - Complete.
- **Inactive:** Suspended / 8. Cancelled / ERCOT - Inactive (Cancelled rows stay Cancelled).
  **Cancellation:** Cancelled / 8. Cancelled / ERCOT - Cancelled.
- **Cancelled / Suspended:** FC, operational and investment status = Not reported, need more data to
  estimate; all dates and amounts cleared; no estimates; auto comment blocks removed (non-QC).
- **Non-QC rows:** downgrades allowed; Cancelled rows reappearing in Large Gen only flagged; project
  name, capacity and sub-location never overwritten (capacity flagged if >10% off or "MW Size");
  SPV/POI filled if blank; IA date overwritten for Early/Late, filled if blank for later stages.
- **QC rows:** lifecycle changes only if it moves up; investment never re-estimated; only the
  "Estimated development timeline" paragraph is replaced.
- **Estimates** (existing rows: on lifecycle change or blank / Not reported status; Reported never touched):
  - Early FC = today + MW/50 x 8 months (min 6); Late FC = today + MW/50 x 6 months (min 3); FC max 3 years.
  - COD = FC + MW/50 x 6 months (min 3, wind 6, max 4 years).
  - Operational FC = COD - MW/50 x 6 months (max 3 years back). All dates month-end.
  - Investment $M/MW: wind 1.6 / 1.5 / 1.4, solar 1.2 / 1.1 / 1.0 for <100 / 100-300 / >=300 MW;
    operational by age <5 / 5-10 / >10 yrs: wind 1.6 / 1.7 / 1.8, solar 1.1 / 1.25 / 1.4; battery 1.0.
- **Field rules:** battery Power sales arrangement = Not Applicable; Early/Late PPA signed = No (blank
  only); Operational PPA signed = Yes - Complete; battery Revenue mechanism = Yet to be
  determined/Unavailable (blank only); solar/wind Operational Power sales arrangement = Yet to be
  determined/Unavailable (blank only); Financing secured No > Financing type blank; Estimated status >
  value in the estimated column, reported column blank.
