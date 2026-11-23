"""A reference set's filter, read as conditions rather than passed on as SQL.

A plan or catalog says which rows of a reused table to load with a filter
such as `ACTIVE_FLAG = 'Y'`. Plans are files people edit and share, so the
text is never put into a query as it stands: it is read as conditions joined
by AND, each one a column, a comparison and values. The column is quoted and
every value is a query parameter, so nothing in a filter can run as SQL.

    COLUMN = 'text'          = <> != < <= > >=, against text or a number
    COLUMN IS NULL           IS NOT NULL
    COLUMN IN ('A', 'B')     NOT IN
    ... AND ...

Anything else is refused, naming the filter.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<string>N?'(?:[^']|'')*')
    | (?P<number>-?\d+(?:\.\d+)?)(?![A-Za-z_\d])
    | (?P<op><>|!=|<=|>=|=|<|>)
    | (?P<punct>[(),])
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)
_KEYWORDS = {"AND", "IS", "NOT", "NULL", "IN"}


class FilterError(ValueError):
    """A filter that is not plain conditions on columns."""


def _tokens(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if not text[position:].strip():
            break
        match = _TOKEN.match(text, position)
        if not match or match.end() == position:
            raise FilterError(
                f"filter {text!r}: cannot read {text[position:].strip()[:20]!r}. "
                "A filter is conditions such as ACTIVE_FLAG = 'Y', joined by AND.")
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "word" and value.upper() in _KEYWORDS:
            kind, value = "keyword", value.upper()
        found.append((kind, value))
        position = match.end()
    return found


def _literal(kind: str, value: str) -> Any:
    if kind == "string":
        return value[value.index("'") + 1:-1].replace("''", "'")
    return float(value) if "." in value else int(value)


def parse_filter(text: str) -> tuple[str, list[Any]]:
    """The WHERE condition for `text`, with its values as parameters."""
    tokens = _tokens(text)
    if not tokens:
        raise FilterError("filter is empty")
    position = 0

    def refuse() -> FilterError:
        seen = tokens[position][1] if position < len(tokens) else "the end"
        return FilterError(f"filter {text!r}: did not expect {seen!r}. A filter is "
                           "conditions such as ACTIVE_FLAG = 'Y', joined by AND.")

    def take(*kinds: str) -> tuple[str, str]:
        nonlocal position
        if position >= len(tokens) or tokens[position][0] not in kinds:
            raise refuse()
        position += 1
        return tokens[position - 1]

    def peek(value: str) -> bool:
        return position < len(tokens) and tokens[position][1] == value

    def accept(value: str) -> bool:
        nonlocal position
        if peek(value):
            position += 1
            return True
        return False

    def expect(value: str) -> None:
        if not accept(value):
            raise refuse()

    parts: list[str] = []
    params: list[Any] = []
    while True:
        _, column = take("word")
        quoted = f"[{column}]"
        if accept("IS"):
            negated = accept("NOT")
            expect("NULL")
            parts.append(f"{quoted} IS {'NOT ' if negated else ''}NULL")
        elif peek("NOT") or peek("IN"):
            negated = accept("NOT")
            expect("IN")
            expect("(")
            values = [_literal(*take("string", "number"))]
            while accept(","):
                values.append(_literal(*take("string", "number")))
            expect(")")
            parts.append(f"{quoted} {'NOT ' if negated else ''}IN "
                         f"({', '.join('?' for _ in values)})")
            params.extend(values)
        else:
            _, op = take("op")
            parts.append(f"{quoted} {'<>' if op == '!=' else op} ?")
            params.append(_literal(*take("string", "number")))
        if position == len(tokens):
            return " AND ".join(parts), params
        expect("AND")
