"""All business rules from the ERCOT automation SOP, kept in one place.

Anything the SOP left open is marked `# DEFAULT` so it is easy to find and change.
"""

DATE_FMT = "%m-%d-%Y"  # Quickbase export/import format

# ---------------------------------------------------------------- DB columns
DB_COLUMNS = [
    "Project ID", "Project name", "Ownership", "Interconnection ID", "Interconnection signed date",
    "Power Capacity (MW)", "Sub-sector", "Sector", "Grid connection approved", "EIA approval received",
    "Project life cycle", "Financial close date status", "Financial close date (reported)",
    "Financial close (estimated)", "Operational date status", "Operational date (estimated)",
    "Operational date (reported)", "Project investment status", "Project investment ($M) - Estimated",
    "Project investment ($M) - Reported", "Financing secured", "Financing type", "PPA signed",
    "Power sales arrangement", "Revenue mechanism", "Sub-location", "State/Region", "Country",
    "Offshore/Onshore", "Power market", "ERCOT Weather Zone", "NERC Region", "NERC Sub-region",
    "Utility service area", "Grid connection status", "Grid connection status (reported by ISO)",
    "Show POI", "POI", "SPV", "Show SPV", "Co-located storage", "Show analyst comment",
    "Analyst comments", "Lead QC",
]
NEW_COLUMNS = DB_COLUMNS + ["Solar technology", "Continent"]

# ---------------------------------------------------------------- ERCOT source
ERCOT_REPORT_TYPE_ID = 15933            # EMIL PG7-200-ER "GIS Report"
ERCOT_NAME_PREFIX = "GIS_Report_"       # e.g. GIS_Report_August2026

# ---------------------------------------------------------------- ERCOT sheets
SHEET_LARGE = "Project Details - Large Gen"
SHEET_SMALL = "Project Details - Small Gen"
SHEET_COMM = "Commissioning Update"
SHEET_INACTIVE = "Inactive Projects"
SHEET_CANCEL = "Cancellation Update"

LARGE_GEN_COLS = [
    "INR", "Project Name", "GIM Study Phase", "Interconnecting Entity", "POI Location", "County",
    "CDR Reporting Zone", "Projected COD", "Fuel", "Technology", "Capacity (MW)", "Change indicators",
    "INA-to-PLN Site Control Date", "Screening Study Started", "Screening Study Complete",
    "FIS Requested", "FIS Approved", "Economic Study Required", "IA Signed",
    "Financial Security and NtP", "Air Permit", "GHG Permit", "Water Availability", "Meets PG 6.9(1)",
    "Meets All PG 6.9", "Meets PG QSA", "Construction Start", "Construction End",
    "Approved for Energization", "Approved for Synchronization", "Comment",
]

TECH_MAP = {  # Large Gen "Technology"
    "WT": ("Wind", "Wind: Onshore"),
    "PV": ("Solar", "Solar: Utility-scale"),
    "BA": ("Battery", "Battery: Lithium-ion"),
}
FUEL_MAP = {  # Commissioning / Inactive / Cancellation "Fuel"
    "Battery Storage": ("Battery", "Battery: Lithium-ion"),
    "Wind": ("Wind", "Wind: Onshore"),
    "Solar": ("Solar", "Solar: Utility-scale"),
}

# ---------------------------------------------------------------- GIM phase
IA_EXECUTED = "6. IA Executed"
GIM_MAP = {
    "SS Completed, FIS Started, No IA": "4. Facility Study",
    "SS Completed, FIS Started, IA": IA_EXECUTED,
    "SS Completed, FIS Completed, IA": IA_EXECUTED,
    "SS Completed, FIS Completed, No IA": "5. In Progress/Unknown",
    "SS Completed, FIS Not Started, IA": IA_EXECUTED,
    "SS Started, FIS Started, No IA": "3. SIS/Cluster Study",
}
# DEFAULT: ISO field = "ERCOT - " + GIM phase (SOP table had a copy slip for "SS Started...")
def iso_status(gim: str) -> str:
    return f"ERCOT - {gim}"

# ---------------------------------------------------------------- lifecycles
EARLY, LATE, OPER = "Early-stage", "Late-stage", "Operational"
CANCELLED, SUSPENDED = "Cancelled", "Suspended"
ADVANCED = {"Ready-to-build", "In construction", "Construction complete", OPER}  # never downgraded
DEV_STAGES = {EARLY, LATE, "Ready-to-build", "In construction", "Construction complete"}

NR = "Not reported, need more data to estimate"

# Lifecycle order: QC rows only take a lifecycle change when it moves UP this ladder
LIFECYCLE_RANK = {CANCELLED: 0, SUSPENDED: 0, EARLY: 1, LATE: 2, "Ready-to-build": 3,
                  "In construction": 4, "Construction complete": 5, OPER: 6}

OPERATIONAL_SET = {
    "Project life cycle": OPER,
    "Grid connection approved": "Yes",
    "EIA approval received": "Yes",
    "Operational date status": "Reported",
    "Financing secured": "Yes - Complete",
    "Financing type": "Debt;Equity",
    "PPA signed": "Yes - Complete",
    "Grid connection status": IA_EXECUTED,
    "Grid connection status (reported by ISO)": "ERCOT - SS Completed, FIS Completed, IA",
}
CANCELLED_SET = {
    "Project life cycle": CANCELLED,
    "Grid connection approved": "No",
    "EIA approval received": "No",
    "Financing secured": "No",
    "Financing type": "",
    "PPA signed": "No",
    "Grid connection status": "8. Cancelled",
    "Grid connection status (reported by ISO)": "ERCOT - Cancelled",
}
# DEFAULT: Inactive = Suspended / ERCOT - Inactive for BOTH existing and new rows
# (matches the new-project spec and current DB convention)
INACTIVE_SET = dict(CANCELLED_SET, **{
    "Project life cycle": SUSPENDED,
    "Grid connection status (reported by ISO)": "ERCOT - Inactive",
})

# ---------------------------------------------------------------- estimation
FC_PER_50MW = {EARLY: 8, LATE: 6}          # months per 50 MW
FC_MIN_MONTHS = {EARLY: 6, LATE: 3}
FC_MAX_MONTHS = 36                         # user decision: FC never more than 3 years out
COD_PER_50MW = 6
COD_MIN_MONTHS = {"Wind": 6}               # others: 3
COD_MIN_DEFAULT = 3
COD_MAX_MONTHS = 48
BACKDATE_PER_50MW = 6
BACKDATE_MAX_MONTHS = 36

# DEFAULT: capacity tiers for "bigger project = lower $/MW"
DEV_METRIC = {  # $M/MW  (<100 MW, 100-300 MW, >=300 MW)
    "Wind": (1.6, 1.5, 1.4),
    "Solar": (1.2, 1.1, 1.0),
    "Battery": (1.0, 1.0, 1.0),
}
OPER_METRIC = {  # (<5 yrs, 5-10 yrs, >10 yrs)
    "Wind": (1.6, 1.7, 1.8),
    "Solar": (1.1, 1.25, 1.4),
    "Battery": (1.0, 1.0, 1.0),
}
CAP_TIERS = (100, 300)

SECTOR_COMMENT_NAME = {
    ("Wind", "Wind: Onshore"): "onshore wind",
    ("Wind", "Wind: Offshore"): "offshore wind",
}

# ---------------------------------------------------------------- new-project defaults
NEW_STATIC = {
    "State/Region": "Texas",
    "Country": "United States of America",
    "Continent": "North America",
    "Offshore/Onshore": "Onshore",
    "Power market": "ERCOT",
    "Co-located storage": "No",
    "Lead QC": "no",
}
PSA_TERMINAL = "Not Applicable"  # DEFAULT: Power sales arrangement for new Cancelled/Suspended
BATTERY_REVENUE_TERMINAL = "Not Applicable"  # DEFAULT: new Cancelled/Suspended batteries

# Commissioning categories treated as Operational (Synchronization Approved is ignored)
COMMISSIONING_SKIP = ("Synchronization Approved",)

# DEFAULT: capacity is never overwritten; flag when ERCOT differs by more than this share
# (or ERCOT's change indicator says "MW Size")
CAPACITY_FLAG_PCT = 0.10

# Source precedence when one INR sits in several ERCOT sheets
PRECEDENCE = ["cancellation", "commissioning", "inactive", "large_gen"]
SOURCE_LABEL = {"cancellation": SHEET_CANCEL, "commissioning": SHEET_COMM,
                "inactive": SHEET_INACTIVE, "large_gen": SHEET_LARGE}
