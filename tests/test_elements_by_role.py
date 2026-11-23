"""A run's rows, read as records by role, exactly as before the rewrite.

elements.run_members and run_claim_lines once named members, subscribers,
claims and claim lines outright; they now find each by its role in the pack
(primary, parent_of_primary, child, lines) and join on the pack's plan.joins.
Under the health pack the records must be what they were, value for value.

The plan is the four record tables' bindings (column -> element), kept from a
real FACETS plan in tests/fixtures/elements-by-role/bindings.json; the run is
made-up rows that join, and some that do not: a member whose subscriber is not
in the run, a claim whose member is not, a line whose claim is not, and empty
values. golden.jsonl was written by the code before the rewrite (f959483).

To record (only when the records are meant to change):
    set SDM_RECORD_BASELINES=1 && pytest tests/test_elements_by_role.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from sdg.writers import elements

KEPT = Path(__file__).parent / "fixtures" / "elements-by-role"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"


def _plan():
    kept = json.loads((KEPT / "bindings.json").read_text(encoding="utf-8"))["tables"]
    return SimpleNamespace(tables=[
        SimpleNamespace(name=name, fields=[SimpleNamespace(name=c, element=e)
                                           for c, e in sorted(bound.items())])
        for name, bound in sorted(kept.items())])


class Run:
    """Rows by table, as a run gives them."""

    def __init__(self, plan):
        self.by_table: dict[str, list[dict]] = {}
        for table in plan.tables:
            columns = [f.name for f in table.fields]
            self.by_table[table.name] = []
            count = {"dbo.CMC_SBSB_SUBSC": 3, "dbo.CMC_MEME_MEMBER": 5,
                     "dbo.CMC_CLCL_CLAIM": 6, "dbo.CMC_CDML_CL_LINE": 12}[table.name]
            for i in range(count):
                row = {c: f"{c}-{i}" for c in columns}
                # Every few rows, a value left empty: not carried to the record.
                if i % 4 == 3:
                    row[columns[-1]] = ""
                self.by_table[table.name].append(row)
        # The joins: two members to a subscriber, the last one's subscriber
        # absent; two claims to a member, the last one's member absent; two
        # lines to a claim, the last one's claim absent.
        for i, row in enumerate(self.by_table["dbo.CMC_MEME_MEMBER"]):
            row["SBSB_CK"] = f"S{i // 2}" if i < 4 else "S-none"
        for i, row in enumerate(self.by_table["dbo.CMC_SBSB_SUBSC"]):
            row["SBSB_CK"] = f"S{i}"
        for i, row in enumerate(self.by_table["dbo.CMC_MEME_MEMBER"]):
            row["MEME_CK"] = f"M{i}"
        for i, row in enumerate(self.by_table["dbo.CMC_CLCL_CLAIM"]):
            row["CLCL_ID"] = f"C{i}"
            row["MEME_CK"] = f"M{i // 2}" if i < 5 else "M-none"
        for i, row in enumerate(self.by_table["dbo.CMC_CDML_CL_LINE"]):
            row["CLCL_ID"] = f"C{i // 2}" if i < 11 else "C-none"

    def rows(self, table: str):
        return iter(self.by_table.get(table, []))


def _records() -> str:
    plan = _plan()
    out = []
    for name, records in (("member", elements.run_members(Run(plan), plan)),
                          ("member, limit 3", elements.run_members(Run(plan), plan, limit=3)),
                          ("line", elements.run_claim_lines(Run(plan), plan)),
                          ("line, limit 4", elements.run_claim_lines(Run(plan), plan, limit=4))):
        out += [json.dumps({"read": name, **record}, sort_keys=True) for record in records]
    return "\n".join(out) + "\n"


def test_records_by_role_are_as_before():
    written = _records()
    golden = KEPT / "golden.jsonl"
    if RECORDING:
        golden.write_text(written, encoding="utf-8")
    expected = golden.read_text(encoding="utf-8")
    assert written == expected
    # What the golden holds, so an empty or one-sided one cannot pass.
    reads = [json.loads(line)["read"] for line in expected.splitlines()]
    assert reads.count("member") == 5 and reads.count("member, limit 3") == 3
    assert reads.count("line") == 12 and reads.count("line, limit 4") == 4
    lines = [json.loads(x) for x in expected.splitlines() if json.loads(x)["read"] == "line"]
    assert lines[0]["MEMBER_KEY"] == "M0" and lines[0]["CLAIM_ID"] == "C0"
    assert "MEMBER_KEY" not in lines[-1], "a line whose claim is absent has no member"
