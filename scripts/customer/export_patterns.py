"""Write every saved pattern to an Excel workbook of one sheet.

    scripts\\customer\\export_patterns.cmd            -> exports\\patterns-<date>-<time>.xlsx
    scripts\\customer\\export_patterns.cmd <file>     -> that file

Reads the pattern tables in the SDM platform database (schema sdm) and the
denorm rows behind each pattern, through the denorm connection in .env.
Writes nothing to any database.

One sheet, one row per:
  pattern value   every field of the pattern (age band, sex, location, group,
                  plan, product; a member pattern's claims: amount, type,
                  diagnosis, procedure; a claims pattern's claim fields and
                  patients), every value, with its count, what it is counted
                  out of and its %
  scenario        each combination of sex, age band and plan (claims: claim
                  type and network) the pattern holds, with its count and its
                  % of the pattern
  tag             the pattern's tags (when the tag table exists)
Every row says whether generation refers to that field. Filter the header
to see one pattern, one field or only the scenarios.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from sdg.discovery.connections import ConnectionRegistry  # noqa: E402


def read(registry: ConnectionRegistry, schema: str):
    with registry.connect("denorm") as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM [{schema}].[patterns] ORDER BY member_count DESC, id")
        cols = [d[0] for d in cur.description]
        patterns = [dict(zip(cols, row)) for row in cur.fetchall()]
        tags = []
        cur.execute(f"SELECT OBJECT_ID(N'[{schema}].[pattern_tags]', N'U')")
        if cur.fetchone()[0] is not None:
            # Columns vary between releases of this table; take what is there.
            cur.execute(f"SELECT * FROM [{schema}].[pattern_tags]")
            tcols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                t = dict(zip(tcols, r))
                tags.append((t.get("pattern_id"), t.get("tag"),
                             t.get("tag_source") or t.get("source"),
                             t.get("confidence"), t.get("confirmed_by")))
            tags.sort(key=lambda t: (t[0] or 0, str(t[1])))
    return patterns, tags


def with_tags(reports: list[dict], patterns: list[dict], tags: list[tuple]) -> list[dict]:
    """Each pattern's tags as rows of its own, after its values."""
    for report, p in zip(reports, patterns):
        for pattern_id, tag, source, confidence, confirmed in tags:
            if pattern_id != p.get("id"):
                continue
            said = ", ".join(str(x) for x in (source, confidence, confirmed) if x not in (None, ""))
            report["report"]["rows"].append({
                "field_label": "Tag", "value": tag, "value_label": said, "count": None,
                "of": None, "pct": None, "generation": ""})
    return reports


def main(argv: list[str]) -> int:
    from sdg.analyzer.services import pattern_report

    registry = ConnectionRegistry()
    schema = registry.settings.denorm_schema
    out = Path(argv[1]) if len(argv) > 1 else (
        ROOT / "exports" / f"patterns-{datetime.now():%Y%m%d-%H%M}.xlsx")
    patterns, tags = read(registry, schema)
    if not patterns:
        print(f"No patterns in [{schema}].[patterns] of {registry.settings.denorm_db}; "
              f"nothing to export.")
        return 1
    reports = with_tags(pattern_report.for_saved(patterns), patterns, tags)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pattern_report.workbook(reports))
    print(f"{len(patterns)} patterns written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
