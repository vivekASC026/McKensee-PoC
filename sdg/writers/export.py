"""Export generated rows in the shapes other systems read.

Delimited and fixed-width text, handed out a block of rows at a time.

**Nothing accumulates.** Every function here is a generator. The engine already
holds its rows in memory -- it has to, because a claim total sums claim lines
that do not exist yet when the claim is built -- so the formatter refusing to
hold a second copy is what keeps the peak from doubling.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator


def _text(value: Any) -> str:
    """One canonical text form, matching the CSV writer so exports agree."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    return str(value)


# The date formats a sample can be read as, in strftime terms.
DATE_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "MM/DD/YYYY": "%m/%d/%Y", "YYYYMMDD": "%Y%m%d"}


def layout_value(value: Any, field: dict[str, Any]) -> Any:
    """A value as a layout field writes it: dated the way the sample was.

    A sample whose dates read 01/31/1980 describes a file that writes them that
    way, and a file in ISO dates where the receiver expects that is a file it
    rejects. Anything that is not a date, or will not read as one, is left be.
    """
    fmt = DATE_FORMATS.get(str(field.get("date_format") or ""))
    if fmt is None or value is None or value == "":
        return value
    if isinstance(value, (date, datetime)):
        return value.strftime(fmt)
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime(fmt)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Delimited and fixed-width output
# ---------------------------------------------------------------------------

# Rows per yielded block. One yield per row makes a syscall per row over HTTP
# and dominates the cost at volume; one yield for everything defeats the point
# of streaming. A few hundred rows is the flat part of that curve.
BLOCK = 256


def stream_delimited(
    records: Iterable[dict[str, Any]],
    columns: list[str],
    *,
    delimiter: str = ",",
    header: bool = True,
    newline: str = "\r\n",
) -> Iterator[str]:
    """Delimited text, a block of rows at a time.

    Values are not quoted: the generated data contains no delimiters, and a
    quoting pass over every field at volume costs more than it is worth here.
    A value that does contain one would corrupt the file, so it is replaced
    rather than silently written.
    """
    if header:
        yield delimiter.join(columns) + newline

    block: list[str] = []
    append = block.append
    for record in records:
        append(
            delimiter.join(
                _text(record.get(column)).replace(delimiter, " ").replace("\n", " ")
                for column in columns
            )
        )
        if len(block) >= BLOCK:
            yield newline.join(block) + newline
            block = []
            append = block.append
    if block:
        yield newline.join(block) + newline


def stream_fixed_width(
    records: Iterable[dict[str, Any]],
    fields: list[dict[str, Any]],
    *,
    newline: str = "\r\n",
) -> Iterator[str]:
    """Fixed-width text, a block of rows at a time.

    Text is padded on the right, numbers on the left, which is the convention
    every mainframe-shaped layout in this domain follows.
    """
    # Resolved once rather than per row: at a hundred thousand rows the
    # difference between reading these dict keys here and reading them inside
    # the loop is seconds.
    # A parsed layout records the widest value it saw as `max_width`; a layout
    # written by hand states `length`. Either is the width of the field.
    # A field read from a sample also carries where it started. Those starts
    # keep the gaps the sample had between its columns; a layout defined by
    # hand has none, and its starts are where the widths already put them.
    # A start behind the row so far would overlap a field, so it is ignored.
    spec = [
        (
            field.get("name", ""),
            int(field.get("length") or field.get("max_width") or 0),
            # A YYYYMMDD date reads as a number in a sample, and is still a
            # date: aligned like text, as the sample had it.
            str(field.get("type") or "text").lower()
            in {"number", "numeric", "decimal", "int"}
            and not field.get("date_format"),
            int(field["start"]) if field.get("start") is not None else None,
        )
        for field in fields
    ]

    block: list[str] = []
    append = block.append
    for record in records:
        parts = []
        at = 0
        for name, width, numeric, start in spec:
            if start is not None and start > at:
                parts.append(" " * (start - at))
                at = start
            value = _text(record.get(name))
            if len(value) > width:
                value = value[:width]
            parts.append(value.rjust(width) if numeric else value.ljust(width))
            at += width
        append("".join(parts))
        if len(block) >= BLOCK:
            yield newline.join(block) + newline
            block = []
            append = block.append
    if block:
        yield newline.join(block) + newline
