"""A pattern's counts as a PDF, laid out as the Counts and % panel shows them:
each field with its values, counts and shares, and whether generation refers
to it, then the scenarios. Several patterns follow one another, each from a
new page.

Written here rather than with a PDF library: the offline package sets would
need another wheel for every Python the customer runs (3.15t among them), and
a report of text and rules needs only the PDF's own fonts.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

# US Letter, in points.
WIDTH, HEIGHT = 612, 792
MARGIN = 50
BOTTOM = 56
# The value column ends where the count begins; count and % are right-aligned.
COUNT_RIGHT = WIDTH - MARGIN - 70
PCT_RIGHT = WIDTH - MARGIN
VALUE_WIDTH = COUNT_RIGHT - MARGIN - 70

# Helvetica's widths (thousandths of the size) for the printable ASCII
# characters, from its font metrics; others count as an "n".
_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]


def _width(text: str, size: float, bold: bool = False) -> float:
    total = sum(_WIDTHS[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return total * size / 1000 * (1.06 if bold else 1.0)


def _fit(text: str, size: float, room: float, bold: bool = False) -> str:
    """The text, cut short with an ellipsis where it would run past `room`."""
    if _width(text, size, bold) <= room:
        return text
    while text and _width(text + "...", size, bold) > room:
        text = text[:-1]
    return text.rstrip() + "..."


def _wrap(text: str, size: float, room: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and _width(trial, size) > room:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def _escape(text: str) -> bytes:
    # The standard fonts read WinAnsi (cp1252); what it lacks prints as "?".
    raw = text.replace("—", "-").encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _count(value: Any) -> str:
    return "" if value is None else f"{int(value):,}"


def _pct(value: Any) -> str:
    return "" if value is None else f"{float(value) * 100:.1f}%"


class _Pages:
    """Pages of text and rules, top to bottom, a new page when one is full."""

    def __init__(self, footer: str) -> None:
        self.pages: list[list[bytes]] = []
        self.footer = footer
        self.y = 0.0
        self.new()

    def new(self) -> None:
        self.pages.append([])
        self.y = HEIGHT - MARGIN

    def room(self, height: float) -> None:
        if self.y - height < BOTTOM:
            self.new()

    def text(self, x: float, text: str, size: float = 9, bold: bool = False,
             gray: bool = False, right: bool = False) -> None:
        if right:
            x -= _width(text, size, bold)
        color = b"0.4 g " if gray else b"0 g "
        font = b"/F2" if bold else b"/F1"
        self.pages[-1].append(color + b"BT " + font + b" %.1f Tf %.2f %.2f Td (" % (size, x, self.y)
                              + _escape(text) + b") Tj ET")

    def rule(self, gray: float = 0.82) -> None:
        y = self.y - 4
        self.pages[-1].append(b"%.2f G 0.5 w %d %.2f m %d %.2f l S"
                              % (gray, MARGIN, y, WIDTH - MARGIN, y))

    def down(self, by: float) -> None:
        self.y -= by

    def pdf(self) -> bytes:
        """The pages as one PDF file, each numbered in its footer."""
        total = len(self.pages)
        objects: list[bytes] = []
        kids = []
        first_page = 5
        for n, content in enumerate(self.pages, 1):
            foot = (b"0.4 g BT /F1 7.5 Tf %d 30 Td (" % MARGIN + _escape(self.footer)
                    + b") Tj ET BT /F1 7.5 Tf %.2f 30 Td (" % (
                        WIDTH - MARGIN - _width(f"Page {n} of {total}", 7.5))
                    + _escape(f"Page {n} of {total}") + b") Tj ET")
            stream = b"\n".join([*content, foot])
            page_id = first_page + 2 * (n - 1)
            kids.append(b"%d 0 R" % page_id)
            objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                           b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                           b"/Contents %d 0 R >>" % (WIDTH, HEIGHT, page_id + 1))
            objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        head = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % total,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>",
        ]
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate([*head, *objects], 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(offsets) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(offsets) + 1, xref))
        return bytes(out)


def _fields(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: list[list[dict[str, Any]]] = []
    for row in rows:
        if grouped and grouped[-1][0]["field_label"] == row["field_label"]:
            grouped[-1].append(row)
        else:
            grouped.append([row])
    return grouped


def document(patterns: list[dict[str, Any]], today: Optional[_dt.date] = None) -> bytes:
    """Every pattern's counts (each {name, share, report}) as a PDF."""
    day = (today or _dt.date.today()).strftime("%d %b %Y")
    pages = _Pages(f"Synthetic Data Management - pattern counts - {day}")
    for i, p in enumerate(patterns):
        rep = p["report"]
        if i:
            pages.new()
        pages.text(MARGIN, _fit(str(p["name"]), 15, WIDTH - 2 * MARGIN, True), 15, bold=True)
        pages.down(18)
        basis = rep["size_basis"]
        intro = f"Counted over all {_count(rep['size'])} {basis} of the pattern"
        if p.get("share") is not None:
            intro += f", {_pct(p['share'])} of everyone looked at"
        pages.text(MARGIN, intro + ".", 9.5, gray=True)
        pages.down(13)
        for line in _wrap('Every value is listed, even one only a single person has. '
                          '"Generation" says whether the generated test data follows '
                          "that field's mix.", 9.5, WIDTH - 2 * MARGIN):
            pages.text(MARGIN, line, 9.5, gray=True)
            pages.down(13)
        pages.down(10)

        for rows in _fields(rep["rows"]):
            first = rows[0]
            # A field's heading, its generation line and its first value stay together.
            pages.room(58)
            pages.text(MARGIN, first["field_label"], 11, bold=True)
            of = f"of {_count(first.get('of'))} {first.get('basis') or basis}"
            pages.text(MARGIN + _width(first["field_label"], 11, True) + 5, of, 9, gray=True)
            pages.down(14)
            if first.get("generation"):
                pages.text(MARGIN, f"Generation: {first['generation']}", 8.5, gray=True)
                pages.down(14)
            for r in rows:
                pages.room(14)
                value = str(r["value"])
                if r.get("value_label"):
                    value += f"  {r['value_label']}"
                pages.text(MARGIN, _fit(value, 9, VALUE_WIDTH), 9)
                pages.text(COUNT_RIGHT, _count(r.get("count")), 9, right=True)
                pages.text(PCT_RIGHT, _pct(r.get("pct")), 9, right=True)
                pages.rule()
                pages.down(14)
            pages.down(10)

        if rep["scenarios"]:
            pages.room(60)
            keys = ", ".join(k["label"].lower() for k in rep["scenario_keys"])
            pages.text(MARGIN, f"Scenarios by {keys}", 11, bold=True)
            pages.down(16)
            pages.text(MARGIN, "Scenario", 8.5, bold=True, gray=True)
            pages.text(COUNT_RIGHT, "Count", 8.5, bold=True, gray=True, right=True)
            pages.text(PCT_RIGHT, "% of pattern", 8.5, bold=True, gray=True, right=True)
            pages.rule(0.5)
            pages.down(14)
            for s in rep["scenarios"]:
                pages.room(14)
                pages.text(MARGIN, _fit(s["label"], 9, VALUE_WIDTH), 9)
                pages.text(COUNT_RIGHT, _count(s["count"]), 9, right=True)
                pages.text(PCT_RIGHT, _pct(s["pct"]), 9, right=True)
                pages.rule()
                pages.down(14)
    return pages.pdf()
