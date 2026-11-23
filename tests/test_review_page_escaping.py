"""The review page escapes what it shows.

Table and column names, values and evidence on the page come from the
database being reviewed. A name holding markup is shown as text, never run.
"""

from __future__ import annotations

from sdg.review.page import render

MARKUP = "<script>alert(1)</script>"


def test_markup_from_the_database_is_shown_as_text():
    page = render({
        "title": MARKUP, "subtitle": "", "stats": [], "queue": [], "ratios": [],
        "elements": [], "decisions_path": "work/review-decisions.json",
        "tables": [{"name": f"dbo.{MARKUP}", "row_count": 1, "triggers": [], "columns": [{
            "name": MARKUP, "bucket": "free", "type": "varchar", "nullable": True,
            "measures": [("max", MARKUP)], "top_values": [], "evidence": [],
            "params_preview": MARKUP, "note": MARKUP}]}],
    })
    assert MARKUP not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
