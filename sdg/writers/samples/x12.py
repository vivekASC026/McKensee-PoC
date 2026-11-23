"""X12 read and written for files modeled on a sample: the syntax, never a transaction.

A trading partner's real 834, 835 or 837 already says everything a description in
`edi_formats/` has to spell out by hand -- which loops, which qualifiers, which
codes, what the submitter and receiver are called. So the sample is the model:
the file written has the sample's envelope, header and segments, in the
sample's order and with the sample's values, and only the positions a
generation config names take new values.

Segments, fields, components, the config (the masking tool's segment config,
`genSegments` for `maskSegments`), proposals and filling are shared with
HL7 v2 (`segments.py`). What is X12's own is here: parsing the interchange,
the envelope (ISA/GS/ST; SE01 counting the transaction; GE/IEA), and X12's
codes from the shipped descriptions. What a transaction repeats, what follows
from what and what a place holds are in its profile (`profiles/x12*.json`),
so an 835 or a 270 is a profile, not code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from sdg.writers.samples import engine, segments
from sdg.writers.samples import profile as profiles
from sdg.writers.samples.base import SampleError
from sdg.writers.snip.x12 import Segment, X12Error, parse

FORMAT = "x12"
LABEL = "X12 834 / 835 / 837"
MEDIA = "application/edi-x12"
EXTENSION = ".txt"
VALIDATION = "snip"

# The envelope: X12's own, for every transaction. Never proposed, never filled;
# ST and SE are written by the reader, SE01 counting the transaction.
ENVELOPE = frozenset({"ISA", "IEA", "GS", "GE", "ST", "SE"})

Entry = segments.Entry
at = segments.at
read_config = segments.read_config
write_config = segments.write_config
positions = segments.positions
used = segments.used
entry_applies = segments.entry_applies


def x12_codes() -> dict[tuple[str, int], tuple[str, dict[str, str], str | None]]:
    """X12's codes for an element's values, from the shipped descriptions.

    A rule makes the store's codes -- relationship M, D, S -- and an 834 writes
    X12's -- 18, 19, 01 -- with INS01 following from the same relationship.
    The descriptions in `edi_formats/` already say so, position by position;
    (segment, X12 element number) -> (element, code map, default).
    """
    from sdg.writers.edi_formats import formats

    out: dict[tuple[str, int], tuple[str, dict[str, str], str | None]] = {}
    for fmt in formats():
        specs = [*fmt.header, *fmt.per_record,
                 *(s for g in fmt.groups for s in g.get("segments") or [])]
        for spec in specs:
            for e in spec.get("elements") or []:
                if e.get("map") and e.get("rule") and e.get("position"):
                    out.setdefault((spec["segment"], int(e["position"])),
                                   (str(e["rule"]).upper(), e["map"], e.get("const")))
    return out


# Where a profile's "codes": {"from": ...} reads them.
CODE_SOURCES = {"edi_formats": x12_codes}


class Syntax(segments.Syntax):
    envelope = ENVELOPE


SYNTAX = Syntax()
selector = SYNTAX.selector


@dataclass
class Sample:
    text: str
    element_sep: str
    component_sep: str
    terminator: str
    newline: str
    head: list[Segment]          # ISA, GS
    tail: list[Segment]          # GE, IEA
    body: list[Segment]          # the first transaction, ST..SE
    transaction: str
    version: str
    profile: dict[str, Any]
    anchors: list[dict[str, Segment | None]]   # per body segment: open loop segment by id
    # X12 repetitions are not read apart: a field is written as one value.
    repetition_sep: str | None = None

    @property
    def kind(self) -> str:
        return self.transaction


def detect(text: str) -> bool:
    return text.lstrip("﻿ \r\n\t").startswith("ISA")


def _profile(kind: str) -> dict[str, Any] | None:
    return profiles.for_kind(FORMAT, kind)


def subject(kind: str) -> str:
    """What a file of this kind is written for, as its profile says."""
    return (_profile(kind) or {}).get("subject", "claims")


def facts(sample: Sample) -> tuple[dict[str, int], str]:
    """What the sample holds, counted as its profile says, and as a line for the screen."""
    counts = {name: sum(1 for s in sample.body if selector(sel)(s))
              for name, sel in (sample.profile.get("counts") or {}).items()}
    counts["segments"] = len(sample.body)
    return counts, str(sample.profile.get("summary") or "").format(**counts)


def read_sample(text: str) -> Sample:
    try:
        ix = parse(text)
    except X12Error as exc:
        raise SampleError(str(exc)) from None
    transactions = [tx for g in ix.groups for tx in g.transactions]
    if not transactions:
        raise SampleError("The sample has no ST/SE transaction in it.")
    tx = transactions[0]
    if tx.se is None:
        raise SampleError("The sample's first transaction has no SE: the file is cut short.")
    body = tx.segments
    kind = tx.st.get(1)
    prof = _profile(kind)
    if prof is None:
        names = [f"an {k}" for k in profiles.kinds(FORMAT)]
        known = " or ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)
        raise SampleError(f"The sample is a {kind or 'unknown'} transaction; a file can be "
                          f"modeled on {known} sample.")
    after = text.split(ix.segment_terminator, 1)[1] if ix.segment_terminator in text else ""
    newline = "\r\n" if after.startswith("\r\n") else ("\n" if after.startswith("\n") else "")
    group = ix.groups[0]
    return Sample(
        text=text, element_sep=ix.element_separator, component_sep=ix.component_separator,
        terminator=ix.segment_terminator, newline=newline,
        head=[s for s in (ix.isa, group.gs) if s is not None],
        tail=[s for s in (group.ge, ix.iea) if s is not None],
        body=body, transaction=kind, version=tx.st.get(3) or group.gs.get(8),
        profile=prof, anchors=segments.anchors(body, prof),
    )


def propose(sample: Sample, entries: list[Entry], algorithms: Iterable[str]) -> list[dict[str, Any]]:
    """Every position that holds a value in the sample, and what the config says for it."""
    return segments.propose(SYNTAX, sample, entries, algorithms)


class Reader(segments.Reader):
    """X12 for the engine: one per file written."""

    syntax = SYNTAX

    def codes(self, prof: dict[str, Any]) -> dict:
        source = (prof.get("codes") or {}).get("from")
        return CODE_SOURCES[source]() if source else super().codes(prof)

    # The interchange's own numbers, set by `stream`: the sample's are another
    # file's, and a partner rejects a second file under the same ISA13.
    seed = "edi"

    def _numbered(self, seg: Segment, positions: dict[int, str]) -> str:
        elements = list(seg.elements)
        for position, value in positions.items():
            if position <= len(elements):
                elements[position - 1] = value
        return self.sample.element_sep.join([seg.id, *elements])

    def envelope(self, sample: Sample) -> tuple[list[str], list[str]]:
        """The sample's ISA and GS, with this file's control number, and
        ISA15 T -- test data -- unless SDG_X12_USAGE=P."""
        from sdg.writers.edi_formats.registry import interchange_number, usage_indicator

        number = interchange_number(self.seed)
        head = []
        for seg in sample.head:
            if seg.id == "ISA":
                head.append(self._numbered(seg, {13: f"{number:09d}", 15: usage_indicator()}))
            elif seg.id == "GS":
                head.append(self._numbered(seg, {6: str(number)}))
            else:
                head.append(seg.raw)
        return head, [sample.body[0].raw]

    def closing(self, sample: Sample, count: int) -> list[str]:
        """SE, its SE01 counting ST through SE: what was written, and SE itself."""
        se = sample.body[-1]
        return [sample.element_sep.join(["SE", str(count + 1), *se.elements[1:]])]

    def after(self, sample: Sample) -> list[str]:
        from sdg.writers.edi_formats.registry import interchange_number

        number = interchange_number(self.seed)
        return [self._numbered(seg, {2: f"{number:09d}" if seg.id == "IEA" else str(number)})
                if seg.id in ("GE", "IEA") else seg.raw for seg in sample.tail]


def stream(
    sample: Sample,
    entries: list[Entry],
    records: Iterable[dict[str, Any]],
    rules: dict[str, dict],
    *,
    seed: str = "edi",
    as_of: str = "2026-06-30",
) -> Iterator[str]:
    """The interchange modeled on the sample, written for these records.

    834: a record is a member. 837: a record is a claim line, carrying its
    member's elements; consecutive lines of one member are that member's
    claims, and consecutive lines of one claim are its lines. What repeats is
    the sample's profile's to say; the engine writes it.
    """
    reader = Reader(sample)
    reader.seed = seed
    yield from engine.stream(reader, sample, entries, records, rules,
                             seed=seed, as_of=as_of)
