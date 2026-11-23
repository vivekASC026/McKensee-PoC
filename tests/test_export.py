"""Writing generated rows in the shapes other systems read.

These cover what the analyzer's own generation tests covered — EDI output,
layout output, row selection, reuse between exports — against the engine that
replaced it. Two properties matter more than the formats themselves:

    it streams        peak memory is a block of rows, not the dataset, or an
                      extract of any real size cannot be produced at all.
    it reproduces     the same plan and seed give byte-identical output, which
                      is the property the whole tool rests on and the one a
                      timestamp taken at export time would quietly destroy.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from sdg.api import app as app_module
from sdg.api.app import app
from sdg.writers import export

client = TestClient(app)

MEMBERS = "dbo.CMC_MEME_MEMBER"
LINES = "dbo.CMC_CDML_CL_LINE"


def _members(count: int = 3) -> list[dict]:
    return [
        {
            "MEME_ID": f"AAA{i:08d}",
            "MEME_FIRST_NAME": "Dana",
            "MEME_LAST_NAME": "Okafor",
            "MEME_MID_INIT": "J",
            "MEME_BIRTH_DT": date(1984, 3, 2),
            "MEME_SEX": "F",
            "MEME_MARITAL_STATUS": "M",
            "MEME_SSN": "123-45-6789",
            "MEME_REL": "18",
            "SBSB_ID": f"AAA{i:06d}",
            "COVERAGE_EFF_DT": date(2020, 1, 1),
            "COVERAGE_TERM_DT": None,
            "CSPD_CAT": "MED",
            "PLDS_DESC": "Choice Medical PPO",
            "INS_LINE_CD": "HLT",
            "MAINTENANCE_TYPE": "021",
            "MAINTENANCE_REASON": "AI",
            "BENEFIT_STATUS": "A",
        }
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Delimited and fixed width
# ---------------------------------------------------------------------------

def test_delimited_output_has_a_header_and_one_line_per_row():
    text = "".join(export.stream_delimited(_members(3), ["MEME_ID", "MEME_SEX"]))
    lines = text.strip().split("\r\n")
    assert lines[0] == "MEME_ID,MEME_SEX"
    assert len(lines) == 4
    assert lines[1] == "AAA00000000,F"


def test_a_value_containing_the_delimiter_cannot_corrupt_the_file():
    rows = [{"A": "one,two", "B": "plain"}]
    text = "".join(export.stream_delimited(rows, ["A", "B"], header=False))
    assert text.strip() == "one two,plain"


def test_fixed_width_pads_text_left_and_numbers_right():
    fields = [
        {"name": "MEME_ID", "length": 14, "type": "text"},
        {"name": "CDML_UNITS", "length": 5, "type": "number"},
    ]
    rows = [{"MEME_ID": "AAA1", "CDML_UNITS": 7}]
    line = "".join(export.stream_fixed_width(rows, fields)).rstrip("\r\n")
    assert line == "AAA1".ljust(14) + "7".rjust(5)
    assert len(line) == 19


def test_a_value_wider_than_its_field_is_truncated_not_allowed_to_shift_the_row():
    fields = [{"name": "A", "length": 3, "type": "text"}, {"name": "B", "length": 3}]
    line = "".join(
        export.stream_fixed_width([{"A": "abcdef", "B": "xy"}], fields)
    ).rstrip("\r\n")
    assert len(line) == 6, "every row has to be the same width"
    assert line == "abcxy "


# ---------------------------------------------------------------------------
# Through the endpoint
# ---------------------------------------------------------------------------

@pytest.mark.db
@pytest.mark.parametrize(
    "fmt,subject,opening",
    [
        # A file names elements, never columns.
        ("csv", "members", "MEMBER_ID,"),
        ("csv", "claims", "CLAIM_ID,"),
        ("edi834", "members", "ISA*"),
        ("edi837", "claims", "ISA*"),
    ],
)
def test_every_format_writes_something_a_reader_would_recognize(fmt, subject, opening):
    response = client.post("/api/export", json={
        "format": fmt, "subject": subject, "root_limit": 40, "rows": 50,
    })
    assert response.status_code == 200, response.text
    body = response.content.decode("utf-8")
    assert body.startswith(opening)
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.db
def test_a_row_cap_is_honored():
    response = client.post("/api/export", json={
        "format": "csv", "subject": "members", "root_limit": 200, "rows": 5,
    })
    lines = response.content.decode("utf-8").strip().split("\r\n")
    assert len(lines) == 6, "a header and five rows"


@pytest.mark.db
def test_two_exports_of_one_run_agree():
    """The gap this guards: each export drawing its own population, so the CSV
    and the EDI describe different people."""
    csv = client.post("/api/export", json={
        "format": "csv", "subject": "members", "root_limit": 60, "rows": 10,
    }).content.decode("utf-8")
    edi_text = client.post("/api/export", json={
        "format": "edi834", "subject": "members", "root_limit": 60, "rows": 10,
    }).content.decode("utf-8")

    # Compared on the subscriber id, which both formats carry. The member id
    # is a CSV column but is not a segment of an 834.
    header, first = csv.strip().split("\r\n")[:2]
    subscriber = first.split(",")[header.split(",").index("SUBSCRIBER_ID")]
    assert subscriber in edi_text, "the two exports describe different populations"


@pytest.mark.db
def test_claim_lines_come_out_grouped_by_claim():
    """What an 837 relies on, asserted where it is produced."""
    from sdg.api.service import Service
    from sdg.writers import elements

    service = Service()
    records = list(
        elements.run_claim_lines(
            service.run_for_export(80), service.require_plan(), limit=400,
        )
    )
    assert records, "the run produced no claim lines"
    seen: set[str] = set()
    current = None
    for record in records:
        claim = record["CLAIM_ID"]
        if claim != current:
            assert claim not in seen, f"claim {claim} appears in two separate runs of lines"
            seen.add(claim)
            current = claim


@pytest.mark.db
def test_a_layout_is_saved_pending_and_only_writes_once_approved(second_approver):
    """The whole cycle, because each step alone proves nothing.

    With a second approver configured, a layout that cannot be saved cannot
    be approved, and one that writes before approval makes the approval
    decorative.
    """
    from sdg.analyzer.services import layout_service

    sample = (
        b"MEME_ID,MEME_FIRST_NAME,MEME_SEX\n"
        b"AAA00000001,Dana,F\n"
        b"AAA00000002,Lee,M\n"
    )
    parsed = client.post(
        "/api/layouts/parse", files={"file": ("cycle.csv", sample, "text/csv")}
    )
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert [f["name"] for f in body["fields"]] == [
        "MEME_ID", "MEME_FIRST_NAME", "MEME_SEX",
    ]

    name = "Export cycle test layout"
    saved = client.post("/api/layouts/save", json={
        "name": name, "kind": body["kind"], "delimiter": body.get("delimiter"),
        "fields": body["fields"], "sample_name": "cycle.csv",
        "purpose": "generation",
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "pending_review"

    layout = next(
        row for row in layout_service.list_layouts() if row["name"] == name
    )
    try:
        refused = client.post("/api/export", json={
            "format": "fixed", "layout_id": layout["id"], "root_limit": 40, "rows": 2,
        })
        assert refused.status_code == 409
        assert "approve" in refused.json()["detail"].lower()

        assert client.post(
            "/api/layouts/approve",
            json={"layout_id": layout["id"], "approver": second_approver},
        ).status_code == 200

        written = client.post("/api/export", json={
            "format": "fixed", "subject": "members",
            "layout_id": layout["id"], "root_limit": 40, "rows": 3,
        })
        assert written.status_code == 200, written.text
        lines = [
            line for line in written.content.decode("utf-8").split("\r\n") if line
        ]
        assert lines, "an approved layout wrote nothing"
        # Fixed width means every row is the same width, or the file is unreadable.
        assert len({len(line) for line in lines}) == 1
    finally:
        layout_service.delete_layout(layout["id"])


@pytest.mark.db
def test_a_saved_layout_writes_at_once_when_no_second_approver_is_configured():
    """No second approver: a layout is usable once saved, with no approval step;
    naming one makes the same layout wait for approval."""
    from sdg.analyzer.services import layout_service

    before = client.get("/api/layouts/approval").json()["secondApprover"]
    client.put("/api/layouts/approval", json={"secondApprover": None})
    name = "SDMT layout without approval"
    layout = None
    try:
        listed = client.get("/api/layouts").json()
        assert listed["approvalRequired"] is False
        saved = client.post("/api/layouts/save", json={
            "name": name, "kind": "csv", "delimiter": "|",
            "fields": [{"name": "MEMBER_ID"}, {"name": "FIRST_NAME"}],
            "sample_name": None, "purpose": "generation",
        })
        assert saved.status_code == 200, saved.text
        layout = next(row for row in layout_service.list_layouts() if row["name"] == name)
        asked = {"format": "csv", "subject": "members", "layout_id": layout["id"],
                 "root_limit": 40, "rows": 2}
        written = client.post("/api/export", json=asked)
        assert written.status_code == 200, written.text
        assert written.text.startswith("MEMBER_ID|FIRST_NAME")

        named = client.put("/api/layouts/approval", json={"secondApprover": "SDMT approver"})
        assert named.json() == {"secondApprover": "SDMT approver", "required": True}
        refused = client.post("/api/export", json=asked)
        assert refused.status_code == 409 and "approv" in refused.json()["detail"].lower()
        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout["id"],
                                 "approver": "SDMT approver"}).status_code == 200
        assert layout_service.get_layout(layout["id"])["approved_by"] == "SDMT approver"
        assert client.post("/api/export", json=asked).status_code == 200
    finally:
        client.put("/api/layouts/approval", json={"secondApprover": before})
        if layout is not None:
            layout_service.delete_layout(layout["id"])


@pytest.mark.db
def test_a_layout_format_refuses_without_an_approved_layout():
    response = client.post("/api/export", json={
        "format": "fixed", "subject": "members", "root_limit": 40,
    })
    assert response.status_code == 422
    assert "layout" in response.json()["detail"].lower()


@pytest.mark.db
def test_a_fixed_width_header_names_the_fields():
    """A headed fixed-width sample should not need its fields renamed.

    Without this every field was FIELD_1, FIELD_2, nothing matched a generated
    column, and the extract came out blank -- a 200 and an empty file, which is
    the worst way for it to fail. The names could always be corrected
    afterwards; this is about usually not having to.

    Telling a header from data has no certain answer: a file of surnames has a
    first row that looks exactly like a row of identifiers. So the test is
    narrow in the safe direction. Missing a header leaves the old behavior and
    a rename to do; inventing one would label data as field names and eat a
    row.
    """
    from sdg.analyzer.services.layout_service import parse_fixed

    headed = (
        "MEME_ID         MEME_FIRST_NAME     MEME_SEX\n"
        "ABC00000001     Dana                F       \n"
        "ABC00000002     Lee                 M       \n"
    )
    parsed = parse_fixed(headed)
    assert parsed["header_row"] is True
    assert [f["name"] for f in parsed["fields"]] == [
        "MEME_ID", "MEME_FIRST_NAME", "MEME_SEX",
    ]
    assert parsed["row_count"] == 2, "the header was counted as data"

    # Capitals alone must not be enough. A column of codes is upper-case too,
    # and reading it as a header costs a row and names every field after a
    # value.
    codes = (
        "ABC00000001     Dana                F       \n"
        "ABC00000002     Lee                 M       \n"
    )
    parsed = parse_fixed(codes)
    assert parsed["header_row"] is False, [f["name"] for f in parsed["fields"]]
    assert parsed["row_count"] == 2, "a data row was eaten"
    assert [f["name"] for f in parsed["fields"]][0] == "FIELD_1"

    # Nor words, which is what a file of names looks like.
    words = (
        "Dana            Lovelace            F       \n"
        "Lee             Hopper              M       \n"
    )
    assert parse_fixed(words)["header_row"] is False


def test_a_described_837_is_well_formed_and_takes_its_values_from_the_row():
    """What a description guarantees, now that it names rules.

    The composite is the part worth pinning: CLM05 takes the component
    separator, and the hand-written writer used the element separator, which
    shifts every element after it by two positions. A trading partner's parser
    rejects that; a person reading the file sees something almost right.
    """
    from sdg.writers.edi_formats import load_format, stream_format

    records = [
        {"CLAIM_ID": "C001", "MEMBER_ID": "M1", "DOB": "1980-04-02",
         "SEX": "F", "CHARGE_AMOUNT": "80.00", "DIAGNOSIS_CODE": "E119000",
         "PROCEDURE_CODE": "H5772", "SERVICE_UNITS": "2", "LINE_NUMBER": "1",
         "SERVICE_DATE": "2025-06-01"},
        {"CLAIM_ID": "C001", "MEMBER_ID": "M1", "DOB": "1980-04-02",
         "SEX": "F", "CHARGE_AMOUNT": "170.00", "DIAGNOSIS_CODE": "E119000",
         "PROCEDURE_CODE": "B3530", "SERVICE_UNITS": "1", "LINE_NUMBER": "2",
         "SERVICE_DATE": "2025-06-01"},
    ]

    out = "".join(stream_format(
        load_format("837P"), iter(records), as_of="2026-06-30", seed="t",
    ))
    segments = [line.rstrip("~") for line in out.splitlines() if line.strip()]

    claims = [s for s in segments if s.startswith("CLM*")]
    assert len(claims) == 1, "two lines of one claim opened it twice"
    composite = claims[0].split("*")[5]
    assert composite == "11:B:1", (
        f"CLM05 is a composite and its components take the component "
        f"separator: {claims[0]}"
    )
    assert claims[0].split("*")[1] == "C001", claims[0]

    # One service line per record, carrying what the row said.
    services = [s for s in segments if s.startswith("SV1*")]
    assert len(services) == 2, services
    assert "HC:H5772" in services[0], services[0]
    assert "80.00" in services[0], services[0]

    diagnosis = next(s for s in segments if s.startswith("HI*"))
    assert diagnosis == "HI*ABK:E119000", diagnosis


def test_every_described_format_is_readable_and_names_its_subject():
    """A format nobody can load is worse than one that does not exist."""
    from sdg.writers.edi_formats import formats

    described = formats()
    assert described, "no transaction sets are described"
    for fmt in described:
        assert fmt.transaction and fmt.version
        assert fmt.subject in ("members", "claims"), fmt.subject
        assert fmt.groups or fmt.per_record, f"{fmt.id} describes no segments"


def test_an_835_names_the_patient_and_pays_each_line():
    """The remittance has to carry what a remittance carries."""
    from sdg.writers.edi_formats import load_format, stream_format

    records = [
        {"CLAIM_ID": "C9", "MEMBER_ID": "M9", "FIRST_NAME": "Dana",
         "LAST_NAME": "Hopper",
         "CLAIM_STATUS": "1", "PAID_DATE": "2025-07-02",
         "PROCEDURE_CODE": "H5772", "CHARGE_AMOUNT": "80.00", "SERVICE_UNITS": "2",
         "SERVICE_DATE": "2025-06-01"},
        {"CLAIM_ID": "C9", "MEMBER_ID": "M9", "FIRST_NAME": "Dana",
         "LAST_NAME": "Hopper",
         "CLAIM_STATUS": "1", "PAID_DATE": "2025-07-02",
         "PROCEDURE_CODE": "B3530", "CHARGE_AMOUNT": "170.00", "SERVICE_UNITS": "1",
         "SERVICE_DATE": "2025-06-01"},
    ]
    out = "".join(stream_format(load_format("835"), iter(records), as_of="2026-06-30"))
    segments = [line for line in out.splitlines() if line.strip()]
    ids = [s.split("*")[0] for s in segments]

    assert ids.count("CLP") == 1, "one claim payment per claim"
    assert ids.count("SVC") == 2, "one service payment per line"
    patient = next(s for s in segments if s.startswith("NM1*QC"))
    assert "Hopper" in patient and "Dana" in patient, patient
    assert any(s.startswith("BPR*") for s in segments), "no financial information"


@pytest.mark.db
def test_a_layout_can_be_defined_rather_than_read_from_a_sample():
    """Reading a sample is the quicker route, not the only one.

    It assumes a sample exists and that the thing being described already has
    rows in it. A fixed-width layout is usually a specification agreed before
    any data does -- column, width, position, written in a document -- and a
    delimited one may be the same.

    A defined layout is not a second kind of layout: it saves the same record a
    parsed one does, and this asserts that by writing a file from each and
    checking the widths the definition stated.
    """
    from sdg.analyzer.services import layout_service

    columns = [
        ("MEMBER_ID", 14), ("FIRST_NAME", 12),
        ("LAST_NAME", 14), ("SEX", 2),
    ]
    start = 0
    fields = []
    for name, width in columns:
        fields.append({
            "name": name, "start": start, "length": width,
            "max_width": width, "type": "text",
        })
        start += width

    name = "Defined fixed layout for tests"

    # Deleting a layout archives it rather than removing it, so that an
    # approval somebody gave is not discarded quietly. A second run of this
    # test therefore meets its own archive, which is the system working: the
    # answer is to restore it, not to invent a new name each time and leave a
    # trail of archives behind.
    archived = next(
        (row for row in layout_service.list_layouts(include_archived=True)
         if row["name"] == name and row.get("status") == "archived"),
        None,
    )
    if archived:
        client.post(f"/api/layouts/{archived['id']}/restore")

    saved = client.post("/api/layouts/save", json={
        "name": name, "kind": "fixed", "delimiter": None,
        "fields": fields, "sample_name": None, "purpose": "generation",
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "pending_review", (
        "a defined layout must be reviewed like any other; approving itself "
        "would make the approval decorative"
    )

    layout = next(
        row for row in layout_service.list_layouts() if row["name"] == name
    )
    try:
        client.post("/api/layouts/approve", json={"layout_id": layout["id"]})
        written = client.post("/api/export", json={
            "format": "fixed", "subject": "members", "rows": 3,
            "root_limit": 60, "layout_id": layout["id"],
        })
        assert written.status_code == 200, written.text

        rows = [line for line in written.text.splitlines() if line.strip()]
        assert rows, "the layout wrote nothing"
        expected = sum(width for _, width in columns)
        for row in rows:
            assert len(row) == expected, (
                f"a row is {len(row)} characters and the definition said "
                f"{expected}: {row!r}"
            )
        # And it is not a file of blanks, which is what a layout whose names
        # match no generated column produces.
        assert rows[0].strip(), "every field came out empty"
        assert rows[0][:14].strip(), "the first field is blank"
    finally:
        client.delete(f"/api/layouts/{layout['id']}")


def test_the_834_description_translates_what_the_format_requires():
    """A description names rules; the format still has to translate.

    FACETS writes a relationship as M and X12 wants 18; marital status S means
    I. That translation is part of the format rather than part of the data, so
    it lives in the description beside the position it applies to -- not in the
    rule, which produces what the source holds.

    This replaces a segment-for-segment comparison against the hand-written
    writer. That test was right while a description named columns: both read
    the same row, so both had to agree. A description names rules now, and
    where a row carries no value the rule makes one -- so the two agree only
    where the row has the answer. The hand-written writer also read keys like
    MAINTENANCE_TYPE that are EDI concepts rather than columns of any source,
    and no rule was seeded for those because none should have been.
    """
    from sdg.writers.edi_formats import load_format, stream_format

    records = [
        {"MEMBER_ID": "M1", "SUBSCRIBER_ID": "S1", "RELATIONSHIP_CODE": "M",
         "SSN": "111-22-3333", "LAST_NAME": "Hall",
         "FIRST_NAME": "Barbara", "MIDDLE_INITIAL": "J",
         "DOB": "1976-11-23", "SEX": "M",
         "MARITAL_STATUS": "M"},
        {"MEMBER_ID": "M2", "SUBSCRIBER_ID": "S1", "RELATIONSHIP_CODE": "S",
         "SSN": "444-55-6666", "LAST_NAME": "Ramos",
         "FIRST_NAME": "Lisa", "DOB": "1949-09-05",
         "SEX": "F", "MARITAL_STATUS": "S"},
    ]

    out = "".join(stream_format(
        load_format("834"), iter(records), as_of="2026-06-30", seed="t",
    ))
    segments = [line.rstrip("~") for line in out.splitlines() if line.strip()]

    ins = [s for s in segments if s.startswith("INS*")]
    assert len(ins) == 2, ins
    # The subscriber is Y and 18; the spouse is N and 01.
    assert ins[0].split("*")[1:3] == ["Y", "18"], ins[0]
    assert ins[1].split("*")[1:3] == ["N", "01"], ins[1]

    dmg = [s for s in segments if s.startswith("DMG*")]
    assert dmg[0].split("*")[4] == "M", dmg[0]
    assert dmg[1].split("*")[4] == "I", f"S must translate to I: {dmg[1]}"

    # Values the row carries are the row's, not invented beside them.
    names = [s for s in segments if s.startswith("NM1*IL")]
    assert "Hall" in names[0] and "Barbara" in names[0], names[0]
    # NM108 is 34, so NM109 is the SSN as X12 wants it: nine digits, no dashes.
    assert names[0].endswith("*34*111223333"), names[0]

    # And a date is written as a date.
    birth = dmg[0].split("*")[2]
    assert birth == "19761123", birth


@pytest.mark.db
def test_the_older_format_names_write_a_well_formed_composite():
    """CLM05 is a composite, and the writer that backed `edi837` wrote it flat.

    Its components take the component separator. Written with the element
    separator the segment reads as three separate elements and CLM06 onward
    shift by two positions -- a trading partner's parser rejects the claim, and
    a person reading the file sees something that looks almost right.

    `edi837` and `edi834` are kept as names because callers use them. They
    resolve to the descriptions now, which is the whole point of fixing it
    here rather than leaving two ways to write an 837.
    """
    response = client.post("/api/export", json={
        "format": "edi837", "subject": "claims", "rows": 3, "root_limit": 40,
    })
    assert response.status_code == 200, response.text

    claims = [line for line in response.text.splitlines() if line.startswith("CLM*")]
    assert claims, "no claim segments were written"
    for claim in claims:
        elements = claim.split("*")
        # CLM05 is the fifth element after the segment id.
        composite = elements[5]
        assert ":" in composite, (
            f"CLM05 was written flat, so every element after it is in the "
            f"wrong position: {claim}"
        )
        assert composite.count(":") == 2, composite

    # And the older enrollment name still writes an enrollment.
    enrollment = client.post("/api/export", json={
        "format": "edi834", "subject": "members", "rows": 3, "root_limit": 40,
    })
    assert enrollment.status_code == 200, enrollment.text
    assert "INS*" in enrollment.text and "ST*834*" in enrollment.text


def test_every_segment_ends_on_its_own_line():
    """X12 does not require a line break and every real file has one.

    A single-line file of half a million segments is unreadable in every tool
    a person would open it with, and the first thing anybody does with a
    generated EDI is open it.
    """
    from sdg.writers.edi_formats import load_format, stream_format

    out = "".join(stream_format(
        load_format("834"), iter(_enrollment_rows(3)), as_of="2026-06-30",
    ))
    lines = [line for line in out.splitlines() if line]
    assert len(lines) > 5, out
    for line in lines:
        assert line.endswith("~"), line
        assert "~" not in line[:-1], f"two segments on one line: {line}"


def test_an_interchange_splits_into_transactions_between_groups():
    """A transaction may not end halfway through a claim.

    Its lines belong to the CLM that opened them. A chunk boundary inside one
    would put the rest in a different ST/SE set pointing at a claim that is not
    there -- which parses, and is wrong, and is the kind of thing a trading
    partner finds rather than this tool.

    So the count is of groups, and a group runs to its end whatever the cap
    says.
    """
    from sdg.writers.edi_formats import load_format, stream_format

    records = []
    for claim, lines in (("C1", 3), ("C2", 2), ("C3", 4)):
        for n in range(lines):
            records.append({
                "CLAIM_ID": claim, "MEMBER_ID": "M1",
                "DOB": "1980-01-01", "SEX": "F",
                 "DIAGNOSIS_CODE": "E119000",
                "PROCEDURE_CODE": f"P{n}", "CHARGE_AMOUNT": "10", "SERVICE_UNITS": "1",
                "SERVICE_DATE": "2025-01-01",
            })

    out = "".join(stream_format(
        load_format("837P"), iter(records), as_of="2026-06-30",
        per_transaction=2,
    ))
    lines = [line.rstrip("~") for line in out.splitlines() if line]

    # One interchange, whatever the transaction count.
    assert sum(1 for s in lines if s.startswith("ISA")) == 1
    assert sum(1 for s in lines if s.startswith("IEA")) == 1

    opens = [s for s in lines if s.startswith("ST*")]
    closes = [s for s in lines if s.startswith("SE*")]
    assert len(opens) == 2, opens
    assert len(closes) == len(opens)

    # GE counts the transactions it closes.
    group = next(s for s in lines if s.startswith("GE*"))
    assert group.split("*")[1] == str(len(opens)), group

    # Every claim's lines are in the same transaction as its CLM.
    current, seen = None, {}
    for segment in lines:
        if segment.startswith("ST*"):
            current = segment.split("*")[2]
        elif segment.startswith("CLM*"):
            seen[segment.split("*")[1]] = current
    assert set(seen) == {"C1", "C2", "C3"}, seen
    assert seen["C1"] == seen["C2"], "the cap of two put them apart"
    assert seen["C3"] != seen["C1"], "the cap of two was not applied"

    # Each SE counts its own transaction, not the file.
    for close in closes:
        assert int(close.split("*")[1]) > 1


def test_nothing_to_write_is_said_rather_than_enveloped():
    """An interchange with no transaction in it is not a file anybody reads."""
    import pytest as _pytest

    from sdg.writers.edi_formats import load_format, stream_format

    with _pytest.raises(ValueError, match="no records"):
        "".join(stream_format(load_format("834"), iter([]), as_of="2026-06-30"))


def _enrollment_rows(count):
    return [
        {"MEMBER_ID": f"M{i}", "SUBSCRIBER_ID": f"S{i}", "RELATIONSHIP_CODE": "M",
         "SSN": f"111-22-{i:04d}", "LAST_NAME": "Hall",
         "FIRST_NAME": "B", "DOB": "1976-11-23",
         "SEX": "M", "MARITAL_STATUS": "M",
         "EFFECTIVE_DATE": "2009-10-14"}
        for i in range(1, count + 1)
    ]


# ---------------------------------------------------------------------------
# Elements, rules and patterns
# ---------------------------------------------------------------------------

def _claim(claim, lines, **extra):
    return [
        {"CLAIM_ID": claim, "MEMBER_ID": "M1", "LAST_NAME": "Hall",
         "FIRST_NAME": "Dana", "DOB": "1980-01-01", "SEX": "F",
         "PROCEDURE_CODE": f"P{n}", "CHARGE_AMOUNT": amount,
         "SERVICE_UNITS": "1", "SERVICE_DATE": "2025-01-01", **extra}
        for n, amount in enumerate(lines)
    ]


def test_a_claim_total_is_its_lines_added_up():
    """CLM02 stated beside its lines can disagree with them; summed, it cannot."""
    from sdg.writers.edi_formats import load_format, stream_format

    out = "".join(stream_format(
        load_format("837P"), iter(_claim("C1", ["80.00", "170.50"])),
        as_of="2026-06-30", rules={},
    ))
    claim = next(s for s in out.splitlines() if s.startswith("CLM*"))
    assert claim.split("*")[2] == "250.50", claim


def test_hierarchical_levels_number_themselves():
    """Every subscriber HL was HL*2, which a trading partner's parser rejects."""
    from sdg.writers.edi_formats import load_format, stream_format

    records = (_claim("C1", ["10"]) + _claim("C2", ["10"], MEMBER_ID="M2")
               + _claim("C3", ["10"], MEMBER_ID="M3"))
    out = "".join(stream_format(
        load_format("837P"), iter(records), as_of="2026-06-30", rules={},
    ))
    levels = [s.split("*")[1] for s in out.splitlines() if s.startswith("HL*")]
    assert levels == ["1", "2", "3", "4"], levels
    patient = next(s for s in out.splitlines() if s.startswith("NM1*IL"))
    assert patient.split("*")[3:5] == ["Hall", "Dana"], patient


def test_values_made_from_rules_do_not_repeat_across_transactions():
    """Position is counted across the interchange, not restarted at each ST,
    or every transaction would name the same members."""
    from sdg.writers.edi_formats import load_format, stream_format

    rules = {"SUBSCRIBER_ID": {"gen_method": "pattern", "format_pattern": "[A-Z]{3}[0-9]{6}"}}
    out = "".join(stream_format(
        load_format("834"), iter([{} for _ in range(6)]), as_of="2026-06-30",
        rules=rules, per_transaction=2,
    ))
    assert out.count("ST*834*") == 3
    ids = [s.split("*")[2].rstrip("~") for s in out.splitlines() if s.startswith("REF*0F")]
    assert len(ids) == 6 and len(set(ids)) == 6, ids


def test_a_pattern_becomes_rules_for_the_elements_it_names():
    """A band stays a band and a split stays a split, keyed by element."""
    from sdg.catalog.loader import load_catalog
    from sdg.writers import elements, rule_values

    profile = {
        "continuous": {"member_age": {"mean": 60, "std": 5, "min": 51, "max": 75}},
        "categorical": {"gender": {"F": 0.69, "M": 0.31}},
        "top_diagnoses": {"E119000": 70, "I10X000": 30},
    }
    rules = elements.pattern_rules(profile, load_catalog(), kind="claim",
                                   as_of="2026-06-30")
    assert set(rules) >= {"DOB", "SEX", "DIAGNOSIS_CODE"}, sorted(rules)
    assert rules["SEX"]["values"] == ["F", "M"]

    births = [rule_values.produce({**rules["DOB"], "element": "DOB"},
                                  seed="t", row_index=i) for i in range(300)]
    ages = {2026 - int(b[:4]) - (1 if b[5:] > "06-30" else 0) for b in births}
    assert min(ages) >= 51 and max(ages) <= 75, (min(ages), max(ages))

    sexes = [rule_values.produce({**rules["SEX"], "element": "SEX"},
                                 seed="t", row_index=i) for i in range(2000)]
    share = sexes.count("F") / len(sexes)
    assert 0.64 < share < 0.74, share


def test_a_range_rule_draws_with_the_shape_the_plan_measured():
    """Most charges are near a hundred and a few reach thousands. Drawn evenly
    between 5 and 25,000 the typical charge would be 12,500; drawn as measured
    it is where the real ones are."""
    import statistics

    from sdg.writers import rule_values

    rule = {"element": "CHARGE_AMOUNT", "gen_method": "range", "min_value": 5.0,
            "max_value": 25000.0, "decimals": 2, "distribution": "lognormal",
            "mean_value": 4.5, "stddev_value": 0.9}
    drawn = [float(rule_values.produce(rule, seed="t", row_index=i)) for i in range(5000)]
    assert all(5.0 <= v <= 25000.0 for v in drawn)
    median = statistics.median(drawn)
    assert 80 < median < 100, median  # exp(4.5) is 90
    assert 120 < statistics.mean(drawn) < 150

    even = [float(rule_values.produce({**rule, "distribution": "uniform"},
                                      seed="t", row_index=i)) for i in range(2000)]
    assert statistics.median(even) > 5000, "an even rule should still draw evenly"


def test_a_measured_shape_reaches_the_rule_rather_than_capping_its_range():
    from types import SimpleNamespace

    from sdg.analyzer.services.seed_elements import _parameters

    field = SimpleNamespace(generator="number", params={
        "distribution": "lognormal", "mean": 4.5, "stddev": 0.9,
        "min": 5.0, "max": 25000.0, "decimals": 2,
    })
    out = _parameters(field)
    assert out["max_value"] == 25000.0
    assert (out["distribution"], out["mean_value"], out["stddev_value"]) == ("lognormal", 4.5, 0.9)


@pytest.mark.db
def test_every_rule_is_named_by_its_element():
    """The repository names elements. A column is an alias, never a rule."""
    from sdg.analyzer.services import layout_service

    for rule in layout_service.get_rules():
        if rule.get("element"):
            assert rule["field_name"] == rule["element"], rule["field_name"]


@pytest.mark.db
def test_an_834_for_a_pattern_is_new_members_with_its_values():
    """And the CSV of the same request names the same people."""
    from sdg.analyzer import db

    pattern = next((p for p in db.get_all_patterns() if p.get("profile_json")), None)
    if pattern is None:
        pytest.skip("no stored pattern")
    request = {"subject": "members", "pattern_id": pattern["id"], "members": 5}
    edi_text = client.post("/api/export", json={"format": "edi:834", **request}).text
    csv = client.post("/api/export", json={"format": "csv", **request}).text
    assert edi_text.count("INS*") == 5, edi_text[:400]

    rows = csv.strip().split("\r\n")
    header = rows[0].split(",")
    assert len(rows) == 6
    for row in rows[1:]:
        subscriber = row.split(",")[header.index("SUBSCRIBER_ID")]
        assert subscriber and subscriber in edi_text, subscriber


@pytest.mark.db
def test_a_dependent_read_from_the_store_is_named_and_names_the_subscriber():
    """A dependent's claim is for the dependent, under the subscriber: the
    store's member columns name the patient and its subscriber columns the
    subscriber. A subscriber's own record is read as it always was."""
    from sdg.config import get_settings
    from sdg.writers import elements

    table = f"{get_settings().denorm_schema}.member_denorm"
    service = app_module.service
    rows = service.registry.query("denorm", (
        f"SELECT TOP 1 d.MEME_CK AS dep, d.MEME_FIRST_NAME, d.MEME_LAST_NAME, "
        f"d.SBSB_FIRST_NAME, d.SBSB_LAST_NAME, d.SBSB_ID, s.MEME_CK AS sub "
        f"FROM {table} d JOIN {table} s ON s.SBSB_CK = d.SBSB_CK AND s.MEME_REL = 'M' "
        f"WHERE d.MEME_REL = 'D' ORDER BY d.MEME_CK"), [])
    if not rows:
        pytest.skip("the store holds no dependents")
    row = rows[0]
    dependent, subscriber = elements.store_members(
        [row["dep"], row["sub"]], app_module._store_shape(), service.registry)
    assert dependent["FIRST_NAME"] == row["MEME_FIRST_NAME"]
    assert dependent["LAST_NAME"] == row["MEME_LAST_NAME"]
    assert dependent["SUBSCRIBER_FIRST_NAME"] == row["SBSB_FIRST_NAME"]
    assert dependent["SUBSCRIBER_LAST_NAME"] == row["SBSB_LAST_NAME"]
    assert dependent["SUBSCRIBER_MEMBER_ID"] == f"{row['SBSB_ID'].strip()}00" == subscriber["MEMBER_ID"]
    assert "SUBSCRIBER_LAST_NAME" not in subscriber
    assert subscriber["FIRST_NAME"] == row["SBSB_FIRST_NAME"]


@pytest.mark.db
def test_an_837_for_a_pattern_is_claims_for_the_members_it_holds():
    """A claim is made by somebody who exists: every subscriber in the file is
    a member the pattern holds -- the members a saved pattern recorded, as the
    pattern store holds them."""
    import json

    from sdg.analyzer import db
    from sdg.analyzer.services import pattern_data
    from sdg.analyzer.services.saved_patterns import member_keys
    from sdg.api.app import _store_shape, service
    from sdg.writers import elements

    pattern = next((p for p in db.get_all_patterns() if p.get("profile_json")), None)
    if pattern is None:
        pytest.skip("no stored pattern")
    response = client.post("/api/export", json={
        "format": "edi:837P", "subject": "claims", "pattern_id": pattern["id"],
        "rows": 30,
    })
    assert response.status_code == 200, response.text
    named = {s.split("*")[9].rstrip("~") for s in response.text.splitlines()
             if s.startswith("NM1*IL")}
    assert named

    keys = (member_keys(json.loads(pattern["profile_json"]))
            or pattern_data.rows_for(pattern["id"])["df"]["MEME_CK"].tolist())
    held = {str(m.get("MEMBER_ID") or "").strip()
            for m in elements.store_members(keys, _store_shape(), service.registry)}
    assert named <= held, "a member in the file is not in the pattern"
