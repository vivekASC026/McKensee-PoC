"""Write the business names of the FACETS columns SDM reads, from the FACETS
data dictionary, so screens and reports say "Product ID" rather than PDPD_ID.

    python scripts/build_facets_names.py <Facets_Data_Dictionary.xlsx>

Only the columns SDM extracts are kept (those named in the FACETS extract
definitions and in the store's denorm tables), and only their names: the
dictionary itself stays where it came from. A column's name is the "Logical
Desc" of its own table (PDPD_ID from CMC_PDPD_PRODUCT), else the most common
one. Where two columns end up with the same name ("State", "City"), the owner
is put in front ("Group State"), as it is for a one-word name ("Member Status"). Columns SDM derives itself are named in
`OWN`. Writes the health pack's sdg/packs/health/column_names.json, which
the screens read from the server (/api/pack/column-names).
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "sdg" / "packs" / "health" / "extract" / "FACETS.sql"
OUT = (ROOT / "sdg" / "packs" / "health" / "column_names.json",)

# The owner a column's four-letter prefix stands for, put in front of a name
# two columns share.
OWNER = {
    "GRGR": "Group", "SGSG": "Subgroup", "SBSB": "Subscriber", "SBAD": "Address",
    "MEME": "Member", "MEPE": "Eligibility", "MECB": "Other Coverage", "MEPR": "PCP",
    "MCRE": "Related Entity", "PYPY": "Payor", "PRAD": "Provider Address",
    "PRPR": "Provider", "PDPD": "Product", "CSPI": "Plan", "CSPD": "Product Category",
    "CLCL": "Claim", "CDML": "Claim Line", "CLCB": "Claim COB", "CDCB": "Line COB",
    "MCTN": "Tax", "NWNW": "Network", "IDCD": "Diagnosis", "IPCD": "Procedure",
    "PSCD": "Place of Service", "CLHP": "Hospital Claim",
}

# Columns SDM derives or renames in its extract; not in the dictionary.
OWN = {
    "_age": "Age", "_tenure": "Tenure (months)",
    "_has_pcp": "Has a PCP", "_pcp_specialty": "PCP Specialty", "_pcp_entity": "PCP Type",
    "_pcp_id": "PCP ID", "MEME_ZIP": "Home ZIP Code", "MEME_CITY": "Home City",
    "MEME_STATE": "Home State",
    "ADDR_ROLE": "Address Role", "ADDR_TYPE": "Address Type",
    "FROM_SUBSCRIBER": "Address From Subscriber",
    "IDCD_ID_PRIMARY": "Primary Diagnosis Code", "IDCD_DESC_PRIMARY": "Primary Diagnosis",
    "IDCD_ID_ADMIT": "Admitting Diagnosis Code", "IPCD_MOD1": "First Procedure Modifier",
    "PROC_CODE": "Procedure Code", "DIAG_COUNT": "Number of Diagnoses",
    "CLMD_DIAG_COUNT": "Number of Diagnoses", "CLED_HIPAA_VERSION": "HIPAA Version",
    "CLM_FREQUENCY": "Claim Frequency", "CSPI_HIOS_ID_NVL": "HIOS Plan ID",
    "MEME_MCTR_ETHN_NVL": "Ethnicity Code", "MEME_MCTR_RACE_NVL": "Race Code",
    "PRPR_MCTR_SPEC": "Provider Specialty", "MCTR_SETG": "Setting",
    "AUTH_KIND": "Authorization Kind", "UB_CODE": "UB Code",
    "CODE_KIND": "Code Kind", "CODE_SEQ": "Code Sequence", "CODE_LOW": "Code Range Low",
    "CODE_HIGH": "Code Range High", "VALUE_AMT": "Value Amount", "VALUE_UNITS": "Value Units",
    "FROM_DT": "From Date", "TO_DT": "To Date", "TERM_DT": "Termination Date",
    "BEN_EFF_DT": "Benefit Effective Date", "BEN_TERM_DT": "Benefit Termination Date",
    "ACCUM_PFX": "Accumulator Prefix", "ACCUM_KIND": "Accumulator Kind",
    "ACCUM_DESC": "Accumulator Description", "EMAIL": "Email",
    "IS_PRIMARY": "Primary", "IS_REMIT": "Remittance", "MAP_KIND": "Mapping Kind",
    "NM1_ENTITY_TYPE": "Entity Type", "REL_EFF_DT": "Relation Effective Date",
    "REL_TERM_DT": "Relation Termination Date", "REL_ENTITY": "Related Entity",
    "REL_SOURCE": "Relation Source", "REL_MCTN_ID": "Related Tax ID",
    "REL_PRPR_ID": "Related Provider ID", "REL_PRPR_NAME": "Related Provider Name",
    "REL_PRPR_NPI": "Related Provider NPI", "REL_PRPR_ENTITY": "Related Provider Entity",
    "REL_PRPR_TAXONOMY_CD": "Related Provider Taxonomy",
    # The claims analyzer's own names.
    "member_age": "Member Age", "charge_amount": "Charge Amount", "gender": "Sex",
    "claim_status": "Claim Status", "claim_type": "Claim Type", "network": "Network",
    "service_units": "Service Units", "network_name": "Network Name",
    "network_type": "Network Type", "place_of_service": "Place of Service",
    "provider_specialty": "Provider Specialty", "provider_type": "Provider Type",
}

# The dictionary's word where the customer's screen says it otherwise.
PLAINER = {"Member Contrived Key": "Member Key", "Group Contrived Key": "Group Key",
           "Subscriber Contrived Key": "Subscriber Key", "Subgroup Contrived Key": "Subgroup Key",
           "Class Product Category": "Product Category",
           "Class Product Category Description": "Product Category Description"}


def _wanted() -> set[str]:
    sql = EXTRACT.read_text(encoding="utf-8")
    columns = set(re.findall(r"\b[A-Z]{4}_[A-Z0-9_]+\b", sql))
    try:
        from sdg.config import load_env
        load_env(str(ROOT / ".env"))
        from sdg.analyzer.entity_loader import connect as _conn
        from sdg.analyzer.sql import read_sql
        frame = read_sql("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                         "WHERE TABLE_SCHEMA = 'sdm' AND TABLE_NAME LIKE '%denorm%'", _conn())
        columns |= set(frame["COLUMN_NAME"])
    except Exception as exc:  # the extract definitions alone still name most
        print(f"store not read ({exc}); naming the extract's columns only")
    return columns


def build(dictionary: str) -> dict[str, str]:
    wanted = _wanted()
    book = openpyxl.load_workbook(dictionary, read_only=True)
    seen: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for row in book["Table Column"].iter_rows(min_row=2, values_only=True):
        table, column, desc = row[1], row[2], row[4]
        if column in wanted and desc and str(desc).strip():
            seen[column].append((str(table), " ".join(str(desc).split())))
    names: dict[str, str] = {}
    for column, found in seen.items():
        prefix = column.split("_")[0]
        home = [d for t, d in found if t.split("_")[1:2] == [prefix]]
        name = collections.Counter(home or [d for _, d in found]).most_common(1)[0][0]
        names[column] = PLAINER.get(name, name)
    shared = collections.Counter(n.lower() for n in names.values())
    for column, name in names.items():
        owner = OWNER.get(column.split("_")[0])
        generic = shared[name.lower()] > 1 or " " not in name
        if generic and owner and not name.lower().startswith(owner.lower()):
            names[column] = f"{owner} {name}"
    names.update(OWN)
    return dict(sorted(names.items()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    made = build(sys.argv[1])
    for path in OUT:
        path.write_text(json.dumps(made, indent=1) + "\n", encoding="utf-8")
        print(f"{path.relative_to(ROOT)}: {len(made)} names")
