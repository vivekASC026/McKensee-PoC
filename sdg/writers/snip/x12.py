"""Reading an X12 interchange into its envelopes, transactions and segments.

The delimiters are read from the ISA segment rather than assumed: the element
separator is its fourth character, the component separator its sixteenth
element, and the segment terminator the character after that. A file from
another system uses whichever it chose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    index: int            # position in the interchange, from 1
    id: str
    elements: list[str]   # elements[0] is the first element after the id
    raw: str
    trailing_separator: bool = False

    def get(self, position: int) -> str:
        """Element at an X12 position (1-based), or '' when absent."""
        return self.elements[position - 1] if 0 < position <= len(self.elements) else ""


@dataclass
class Transaction:
    st: Segment
    segments: list[Segment] = field(default_factory=list)   # ST..SE inclusive
    se: Segment | None = None


@dataclass
class Group:
    gs: Segment
    transactions: list[Transaction] = field(default_factory=list)
    ge: Segment | None = None


@dataclass
class Interchange:
    element_separator: str
    component_separator: str
    segment_terminator: str
    isa: Segment | None
    isa_raw: str
    groups: list[Group] = field(default_factory=list)
    iea: Segment | None = None
    segments: list[Segment] = field(default_factory=list)
    stray: list[Segment] = field(default_factory=list)   # outside any envelope


class X12Error(ValueError):
    """The text is not an X12 interchange at all."""


def parse(text: str) -> Interchange:
    body = text.lstrip("﻿ \r\n\t")
    if not body.startswith("ISA") or len(body) < 106:
        raise X12Error("This is not an X12 interchange: it does not begin with a full ISA segment.")
    element = body[3]
    # ISA16 is the component separator; the segment terminator follows it.
    isa_fields = body[:106].split(element)
    if len(isa_fields) < 17:
        raise X12Error("The ISA segment does not have 16 elements.")
    component = isa_fields[16][:1]
    terminator = body[105] if len(body) > 105 else "~"

    pieces = [p.strip("\r\n") for p in body.split(terminator)]
    segments: list[Segment] = []
    for raw in pieces:
        if not raw.strip():
            continue
        parts = raw.split(element)
        segments.append(Segment(
            index=len(segments) + 1, id=parts[0], elements=parts[1:], raw=raw,
            trailing_separator=raw.endswith(element),
        ))

    interchange = Interchange(element, component, terminator, None, body[:106],
                              segments=segments)
    group: Group | None = None
    transaction: Transaction | None = None
    for seg in segments:
        if seg.id == "ISA":
            interchange.isa = seg
        elif seg.id == "IEA":
            interchange.iea = seg
        elif seg.id == "GS":
            group = Group(gs=seg)
            interchange.groups.append(group)
        elif seg.id == "GE":
            if group is not None:
                group.ge = seg
            group = None
        elif seg.id == "ST":
            transaction = Transaction(st=seg, segments=[seg])
            if group is not None:
                group.transactions.append(transaction)
            else:
                interchange.stray.append(seg)
        elif seg.id == "SE":
            if transaction is not None:
                transaction.segments.append(seg)
                transaction.se = seg
            transaction = None
        elif transaction is not None:
            transaction.segments.append(seg)
        else:
            interchange.stray.append(seg)
    return interchange
