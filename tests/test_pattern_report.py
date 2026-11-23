"""A pattern's counts and percentages: every field, every value, every scenario.

From customer feedback: the analysis must speak to age, demographics, claim
amount, procedure and diagnosis codes, plan and product, with counts and
percentages at pattern and scenario level, exported on one sheet, and say how
each field is used in generation. Nothing is hidden: a scenario of one is
listed.

The home addresses this test writes into sdm.member_address are deleted by
the keys it wrote, only those.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.analyzer.services import pattern_data, pattern_report as pr

client = TestClient(app)
pytestmark = pytest.mark.db

SCOPE = {"grgr_ck": [1]}


def _members_pattern():
    analysis = client.post("/api/patterns/analyze", json={"filters": SCOPE, "k": 3}).json()
    return max(analysis["patterns"], key=lambda p: p["size"])


def _by_field(rows):
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["field"], []).append(r)
    return out


def test_a_member_pattern_counts_every_field_and_its_members_claims():
    pattern = _members_pattern()
    rows = pattern_data.every_row("members", SCOPE, 3, pattern["cluster_id"])
    report = pr.report("members", rows)
    assert report["size"] == pattern["size"] == len(rows)
    fields = _by_field(report["rows"])
    for key in ("age_band", "gender", "marital_status", "relationship", "state", "zip",
                "group", "plan_category", "plan", "product", "has_claims"):
        values = fields[key]
        # Every member is counted once per member field: (none) included.
        assert sum(v["count"] for v in values) == report["size"], key
        assert sum(v["pct"] for v in values) == pytest.approx(1.0, abs=1e-4), key
        assert all(v["basis"] == "members" and v["of"] == report["size"] for v in values)
        assert all(v["generation"] for v in values), key
    # The members' claims, counted over all of them -- never per member.
    with_claims = next(v["count"] for v in fields["has_claims"] if v["value"] == "Yes")
    claims = fields["claim_amount"][0]["of"]
    assert with_claims > 0 and claims >= with_claims
    for key in ("claim_amount", "claim_type", "diagnosis"):
        assert {v["basis"] for v in fields[key]} == {"claims"}
        assert sum(v["count"] for v in fields[key]) == claims, key
    assert {v["basis"] for v in fields["procedure"]} == {"claim lines"}
    # Bands read in order; codes say what they are.
    ages = [v["value"] for v in fields["age_band"] if v["value"] != "(none)"]
    assert ages == sorted(ages, key=lambda a: int(a.split("-")[0].rstrip("+")))
    assert {v["value_label"] for v in fields["gender"]} <= {"Female", "Male", "Unknown"}
    assert any(v["value_label"] for v in fields["plan"]), "a plan is named"


def test_every_scenario_is_one_row_and_none_is_hidden():
    pattern = _members_pattern()
    rows = pattern_data.every_row("members", SCOPE, 3, pattern["cluster_id"])
    report = pr.report("members", rows, ["gender", "age_band", "plan"], with_claims=False)
    scenarios = report["scenarios"]
    assert [k["key"] for k in report["scenario_keys"]] == ["gender", "plan", "age_band"]
    assert sum(s["count"] for s in scenarios) == report["size"]
    assert sum(s["pct"] for s in scenarios) == pytest.approx(1.0, abs=1e-4)
    assert len({s["label"] for s in scenarios}) == len(scenarios), "labels tell them apart"
    assert [s["count"] for s in scenarios] == sorted((s["count"] for s in scenarios),
                                                     reverse=True)
    assert set(scenarios[0]) == {"values", "label", "count", "pct"}, "one row, no breakdown"


def test_scenarios_count_every_row_of_a_large_pattern():
    """The rows view holds 20,000; scenarios are counted over all of them."""
    analysis = client.post("/api/patterns/analyze", json={"filters": {}}).json()
    big = max(analysis["patterns"], key=lambda p: p["size"])
    if big["size"] <= pattern_data.MAX_ROWS:
        pytest.skip("No pattern larger than the rows view in this store.")
    listed = client.post("/api/patterns/scenarios", json={
        "domain": "members", "filters": {}, "k": None, "cluster_id": big["cluster_id"],
        "dimensions": ["gender"]}).json()
    assert listed["rows"] == big["size"]
    assert sum(s["rows"] for s in listed["scenarios"]) == big["size"]


def test_a_claims_pattern_counts_claims_and_its_patients():
    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 2}).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    response = client.post("/api/patterns/report", json={
        "domain": "claims", "filters": SCOPE, "k": 2, "cluster_id": pattern["cluster_id"]})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["size_basis"] == "claims"
    fields = _by_field(report["rows"])
    for key in ("claim_type", "claim_amount", "diagnosis", "age_band", "gender", "plan"):
        assert sum(v["count"] for v in fields[key]) == report["size"], key
    assert {v["basis"] for v in fields["procedure"]} == {"claim lines"}
    assert {v["value_label"] for v in fields["claim_type"]} <= {
        "Professional", "Institutional", "Dental", "Medical", "Hospital", ""}
    assert sum(s["count"] for s in report["scenarios"]) == report["size"]


def test_home_addresses_are_counted_by_state_and_zip():
    from sdg.analyzer import db
    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE, SQL_SCHEMA
    from sdg.analyzer.entity_loader import present_columns
    from sdg.analyzer.services import service

    if "SBAD_ZIP" not in present_columns(f"{SQL_SCHEMA}.member_address"):
        pytest.skip("No sdm.member_address in this store.")
    members = service._load(SCOPE, DEFAULT_REFERENCE_DATE)[0]
    keys = [int(k) for k in members["MEME_CK"].head(3)]
    written: list[int] = []
    with db.get_connection() as connection:
        cursor = connection.cursor()
        for key in keys:
            cursor.execute(f"SELECT COUNT(*) FROM {SQL_SCHEMA}.member_address "
                           "WHERE MEME_CK = ? AND ADDR_ROLE = 'HOME'", (key,))
            if cursor.fetchone()[0]:
                continue
            cursor.execute(
                f"INSERT INTO {SQL_SCHEMA}.member_address (MEME_CK, ADDR_ROLE, SBSB_CK, "
                "SBAD_TYPE, FROM_SUBSCRIBER, SBAD_ADDR1, SBAD_CITY, SBAD_STATE, SBAD_ZIP) "
                "VALUES (?, 'HOME', 0, 'H', 'N', '1 TEST ST', 'SDMTESTVILLE', 'ZZ', '00001')",
                (key,))
            written.append(key)
        connection.commit()
    try:
        loaded = service._load(SCOPE, DEFAULT_REFERENCE_DATE, refresh=True)[0]
        mine = loaded[loaded["MEME_CK"].isin(written)]
        assert set(mine["MEME_STATE"]) == {"ZZ"} and set(mine["MEME_ZIP"]) == {"00001"}
        report = pr.report("members", loaded, [], with_claims=False)
        fields = _by_field(report["rows"])
        assert {"value": "ZZ", "count": len(written)}.items() <= next(
            v for v in fields["state"] if v["value"] == "ZZ").items()
        zips = next(v for v in fields["zip"] if v["value"] == "00001")
        assert zips["value_label"] == "SDMTESTVILLE"
    finally:
        with db.get_connection() as connection:
            cursor = connection.cursor()
            for key in written:
                cursor.execute(f"DELETE FROM {SQL_SCHEMA}.member_address WHERE MEME_CK = ? "
                               "AND ADDR_ROLE = 'HOME' AND SBAD_CITY = 'SDMTESTVILLE'", (key,))
            connection.commit()
        service._load(SCOPE, DEFAULT_REFERENCE_DATE, refresh=True)


def test_an_analysis_downloads_as_a_pdf_laid_out_as_the_panel():
    response = client.post("/api/patterns/report.xlsx",
                           json={"domain": "members", "filters": SCOPE, "k": 3,
                                 "format": "pdf"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith('.pdf"')
    body = response.content
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF")
    analysis = client.post("/api/patterns/analyze", json={"filters": SCOPE, "k": 3}).json()
    # One pattern to a page at least, each page numbered.
    pages = body.count(b"/Type /Page ")
    assert pages >= len(analysis["patterns"])
    assert f"Page {pages} of {pages}".encode() in body
    for words in (b"Age band", b"Generation: Yes, referred into data generation",
                  b"Scenarios by"):
        assert words in body, words


def test_the_pdf_cross_reference_points_at_every_object():
    from sdg.analyzer.services import pattern_pdf

    rows = [{"field_label": "Sex", "value": "F", "value_label": "Female (Ms) \\",
             "count": 3, "of": 4, "basis": "members", "pct": 0.75, "generation": pr.YES}]
    body = pattern_pdf.document([{"name": "A (test) pattern", "share": None, "report": {
        "entity": "members", "size": 4, "size_basis": "members", "rows": rows,
        "scenario_keys": [], "scenarios": []}}])
    xref = int(body.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    table = body[xref:].split(b"\n")
    count = int(table[1].split()[1])
    for i, line in enumerate(table[3:3 + count - 1], 1):
        assert body[int(line[:10]):].startswith(b"%d 0 obj" % i)
    # Brackets and backslashes in names do not end the text early.
    assert b"A \\(test\\) pattern" in body and b"Female \\(Ms\\) \\\\" in body


def test_an_analysis_downloads_as_one_sheet():
    from openpyxl import load_workbook

    response = client.post("/api/patterns/report.xlsx",
                           json={"domain": "members", "filters": SCOPE, "k": 3})
    assert response.status_code == 200, response.text
    book = load_workbook(io.BytesIO(response.content))
    assert book.sheetnames == ["Patterns"], "one tab"
    rows = list(book.active.iter_rows(values_only=True))
    assert list(rows[0]) == pr.SHEET_COLUMNS
    body = rows[1:]
    analysis = client.post("/api/patterns/analyze", json={"filters": SCOPE, "k": 3}).json()
    assert {r[1] for r in body} == {p["name"] for p in analysis["patterns"]}
    level = pr.SHEET_COLUMNS.index("Level")
    count, of, pct = (pr.SHEET_COLUMNS.index(c) for c in ("Count", "Out of", "%"))
    assert {r[level] for r in body} == {"Pattern", "Scenario"}
    for r in body:
        assert r[pct] == pytest.approx(r[count] / r[of], abs=1e-5)
    assert book.active.cell(row=2, column=pct + 1).number_format == "0.0%"
    used = pr.SHEET_COLUMNS.index("Used in generation")
    assert all(r[used] for r in body), "every row says how generation uses it"


# ---------------------------------------------------------------------------
# The fields counted, chosen on the Config screen
# ---------------------------------------------------------------------------

@pytest.fixture()
def kept_choice():
    """Whatever field choice was stored before, put back afterwards."""
    from sdg.analyzer.services import report_fields

    before = {e: report_fields._stored(e) for e in ("members", "claims")}
    yield
    for entity, stored in before.items():
        if stored is None:
            report_fields.reset(entity)
        else:
            report_fields.choose(entity, stored)


def test_fields_can_be_switched_off_and_denorm_columns_added(kept_choice):
    offered = client.get("/api/report-fields").json()
    members = offered["members"]
    builtin = [f["key"] for f in members if f["builtin"]]
    assert {"age_band", "gender", "plan", "product", "diagnosis"} <= set(builtin)
    columns = {f["key"] for f in members if not f["builtin"]}
    assert "col:GRGR_STATE" in columns and "col:SBSB_ORIG_EFF_DT" in columns
    assert not {"col:MEME_SSN", "col:MEME_FIRST_NAME", "col:MEME_BIRTH_DT"} & columns, \
        "personal columns are not offered"

    chosen = [k for k in builtin if k not in ("zip", "procedure")]
    chosen += ["col:GRGR_STATE", "col:SBSB_ORIG_EFF_DT"]
    saved = client.put("/api/report-fields", json={"entity": "members", "fields": chosen})
    assert saved.status_code == 200, saved.text
    assert [f["key"] for f in saved.json()["members"] if f["chosen"]] == [
        k for k in [f["key"] for f in saved.json()["members"]] if k in chosen]

    pattern = _members_pattern()
    report = client.post("/api/patterns/report", json={
        "domain": "members", "filters": SCOPE, "k": 3,
        "cluster_id": pattern["cluster_id"]}).json()
    fields = _by_field(report["rows"])
    assert "zip" not in fields and "procedure" not in fields, "switched off"
    for key in ("col:GRGR_STATE", "col:SBSB_ORIG_EFF_DT"):
        assert sum(v["count"] for v in fields[key]) == report["size"], key
        assert {v["generation"] for v in fields[key]} == {pr.NO}
    assert fields["col:GRGR_STATE"][0]["field_label"] == "Group State", "named as FACETS does"
    assert fields["col:SBSB_ORIG_EFF_DT"][0]["field_label"] == "Subscriber Original Effective Date"
    years = [v["value"] for v in fields["col:SBSB_ORIG_EFF_DT"] if v["value"] != "(none)"]
    assert years == sorted(years) and all(len(y) == 4 for y in years), "a date by its year"

    refused = client.put("/api/report-fields",
                         json={"entity": "members", "fields": ["col:MEME_SSN"]})
    assert refused.status_code == 422

    back = client.put("/api/report-fields", json={"entity": "members", "fields": [],
                                                  "reset": True}).json()["members"]
    assert [f["key"] for f in back if f["chosen"]] == builtin


def test_a_claim_line_column_is_counted_per_line_and_numbers_in_bands(kept_choice):
    from sdg.analyzer.services import report_fields

    builtin = [f["key"] for f in report_fields.offered("claims") if f["builtin"]]
    report_fields.choose("claims", [*builtin, "col:CDML_CHG_AMT", "col:CLCL_PAID_DT"])
    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 2}).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    rows = pattern_data.every_row("claims", {"grgr_ck": [1]}, 2, pattern["cluster_id"])
    fields = _by_field(pr.report("claims", rows)["rows"])
    allowed = fields["col:CDML_CHG_AMT"]
    assert {v["basis"] for v in allowed} == {"claim lines"}
    assert sum(v["count"] for v in allowed) == len(rows)
    bands = [v for v in allowed if v["value"] != "(none)"]
    assert 2 <= len(bands) <= 10, "many amounts, ten bands at most"
    starts = [float(v["value"].split("-")[0].replace(",", "")) for v in bands]
    assert starts == sorted(starts)
    assert {v["basis"] for v in fields["col:CLCL_PAID_DT"]} == {"claims"}


# ---------------------------------------------------------------------------
# Exported from the screen: one pattern, or kept patterns, Excel or CSV
# ---------------------------------------------------------------------------

def test_one_pattern_exports_as_its_panel_shows_it_in_excel_or_csv():
    import csv as _csv

    from openpyxl import load_workbook

    pattern = _members_pattern()
    body = {"domain": "members", "filters": SCOPE, "k": 3,
            "cluster_id": pattern["cluster_id"], "name": pattern["name"]}
    report = client.post("/api/patterns/report", json=body).json()
    sheet = client.post("/api/patterns/report.xlsx", json=body)
    assert sheet.status_code == 200, sheet.text
    assert ".xlsx" in sheet.headers["content-disposition"]
    rows = list(load_workbook(io.BytesIO(sheet.content)).active.iter_rows(values_only=True))
    assert {r[1] for r in rows[1:]} == {pattern["name"]}
    assert len(rows) - 1 == len(report["rows"]) + len(report["scenarios"]), \
        "exactly what the panel shows"

    text = client.post("/api/patterns/report.xlsx", json={**body, "format": "csv"})
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/csv")
    assert ".csv" in text.headers["content-disposition"]
    lines = list(_csv.reader(io.StringIO(text.content.decode("utf-8-sig"))))
    assert lines[0] == pr.SHEET_COLUMNS and len(lines) == len(rows)


def test_kept_patterns_export_by_choice():
    from openpyxl import load_workbook

    from sdg.analyzer import db

    kept = db.get_all_patterns()
    if len(kept) < 2:
        pytest.skip("Fewer than two kept patterns in this store.")
    chosen = [int(kept[0]["id"]), int(kept[1]["id"])]
    sheet = client.post("/api/patterns/report.xlsx", json={
        "domain": "members", "pattern_ids": chosen})
    assert sheet.status_code == 200, sheet.text
    rows = list(load_workbook(io.BytesIO(sheet.content)).active.iter_rows(values_only=True))
    names = {str(p.get("contextual_name")) for p in kept[:2]}
    assert {r[1] for r in rows[1:]} == names


def test_columns_are_shown_by_their_facets_names():
    from sdg.analyzer.facets_names import name
    assert name("PDPD_ID") == "Product ID" and name("PLDS_DESC") == "Plan Description"
    assert name("CSPI_ID") == "Plan ID" and name("CLCL_TOT_CHG") == "Claim Total Charge"
    assert name("GRGR_STATE") != name("SBAD_STATE"), "two States say whose"
    assert name("NOT_A_COLUMN") == "NOT_A_COLUMN", "unknown columns show as they are"


def test_age_band_has_no_gaps_between_bands():
    """Member ages carry fractions (17.4 years); every age falls in a band."""
    from sdg.analyzer.services.pattern_report import _age_band
    from sdg.analyzer.services.scenarios import _band

    for read in (_age_band, _band):
        assert read(17.4) == "0-17"
        assert read(34.9) == "18-34"
        assert read(49.5) == "35-49"
        assert read(64.99) == "50-64"
        assert read(-0.2) == "0-17"
        assert read(None) is None
        assert read(float("nan")) is None


def test_the_split_chosen_is_the_split_downloaded():
    """Unticking features and ticking others changes the scenarios in the
    counts and in every download, Excel, CSV and PDF alike; with no choice
    sent, the Scenarios panel's own default is used."""
    pattern = _members_pattern()
    body = {"domain": "members", "filters": SCOPE, "k": 3,
            "cluster_id": pattern["cluster_id"], "name": pattern["name"]}

    default = client.post("/api/patterns/report", json=body).json()
    defaults = client.get("/api/patterns/scenario-dimensions?entity=members").json()["defaults"]
    assert [k["key"] for k in default["scenario_keys"]] == defaults == ["gender", "age_band"]

    marital = {**body, "scenario_keys": ["marital_status"]}
    report = client.post("/api/patterns/report", json=marital).json()
    assert [k["key"] for k in report["scenario_keys"]] == ["marital_status"]
    assert all(s["label"].startswith("Marital status") for s in report["scenarios"])

    text = client.post("/api/patterns/report.xlsx", json={**marital, "format": "csv"})
    body_text = text.content.decode("utf-8-sig")
    assert "Marital status" in body_text and "Gender F" not in body_text
    before = client.post("/api/patterns/report.xlsx", json={**body, "format": "csv"})
    assert "Gender F" in before.content.decode("utf-8-sig")

    pdf_marital = client.post("/api/patterns/report.xlsx", json={**marital, "format": "pdf"})
    pdf_default = client.post("/api/patterns/report.xlsx", json={**body, "format": "pdf"})
    assert pdf_marital.status_code == pdf_default.status_code == 200
    assert pdf_marital.content != pdf_default.content
    assert b"Marital status" in pdf_marital.content and b"Gender F" not in pdf_marital.content


def test_every_pattern_of_an_analysis_downloads_with_the_split_chosen():
    marital = {"domain": "members", "filters": SCOPE, "k": 3,
               "scenario_keys": ["marital_status"], "format": "csv"}
    text = client.post("/api/patterns/report.xlsx", json=marital).content.decode("utf-8-sig")
    assert "Marital status" in text and "Gender F" not in text
