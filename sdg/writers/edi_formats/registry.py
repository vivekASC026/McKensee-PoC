"""Read a transaction set description and emit it, a block at a time.

The emitter holds one block of segments, never the dataset, for the same reason
every other writer here does: an extract is the one thing whose size is not
bounded by a screen.

Records arrive grouped. A group opens when the value of its `opens_on` element
changes -- a new claim, a new member -- and its segments are written once per
group, while segments outside any group are written once per record. That is
the whole of the structure, because an X12 transaction is a header, repeated
groups of repeated detail, and a trailer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

ELEMENT_SEP = "*"
COMPOSITE_SEP = ":"
# A segment terminator, and then a line break. X12 does not require one -- the
# terminator is what ends a segment -- but every file anybody actually exchanges
# has them, and a single-line file of half a million segments is unreadable in
# every tool a person would open it with.
SEGMENT_TERM = chr(126) + chr(10)

# Segments per yielded block, as elsewhere: enough that the cost of yielding
# disappears, few enough that memory does not follow the row count.
BLOCK = 512

HERE = Path(__file__).resolve().parent


@dataclass
class EdiFormat:
    id: str
    name: str
    transaction: str
    subject: str
    version: str
    description: str = ""
    functional_code: str = "HC"
    header: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    per_record: list[dict] = field(default_factory=list)
    # What a remittance works out a claim paid with; see writers/adjudication.
    adjudication: dict | None = None
    path: Path | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "transaction": self.transaction,
            "subject": self.subject,
            "version": self.version,
            "description": self.description,
        }


def formats() -> list[EdiFormat]:
    """Every transaction set described in this directory."""
    from sdg import packs

    packs.require("x12", "X12 EDI")
    found = []
    for path in sorted(HERE.glob("*.json")):
        try:
            found.append(load_format(path.stem))
        except Exception:
            # A malformed description should not hide the ones beside it.
            continue
    return found


def load_format(name: str) -> EdiFormat:
    from sdg import packs

    packs.require("x12", "X12 EDI")
    # Only a description that sits in this directory: the name comes from a
    # request, and one such as "..\\x" must not reach a path outside it.
    known = sorted(p.stem for p in HERE.glob("*.json"))
    stem = next((k for k in known if k.upper() == str(name).upper()), None)
    if stem is None:
        raise FileNotFoundError(
            f"No EDI format {name!r}. Choose one of: {', '.join(known) or 'none'}.")
    path = HERE / f"{stem}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("adjudication"):
        # A file that says what each claim paid (the 835) is adjudicated.
        packs.require("adjudication", f"{body['name']} (paid claims)")
    return EdiFormat(
        id=body.get("id") or path.stem,
        name=body["name"],
        transaction=body["transaction"],
        subject=body.get("subject", "members"),
        version=body["version"],
        description=body.get("description", ""),
        functional_code=body.get("functional_code", "HC"),
        header=body.get("header") or [],
        groups=body.get("groups") or [],
        per_record=body.get("per_record") or [],
        adjudication=body.get("adjudication"),
        path=path,
    )


def usage_indicator() -> str:
    """ISA15: T, test data, unless SDG_X12_USAGE=P says the files are production.

    A synthetic file marked P is one a partner's system takes as real."""
    import os

    from sdg.config import load_env

    load_env()
    return "P" if os.getenv("SDG_X12_USAGE", "").strip().upper() == "P" else "T"


def interchange_number(seed: str) -> int:
    """ISA13 and GS06: one number per file's seed, 1 to 999,999,999.

    The same seed writes the same file, so a file written again has its number;
    another request's file has another, and a partner does not reject the
    second file of a day as a duplicate of the first."""
    import hashlib

    return int(hashlib.sha256(f"interchange:{seed}".encode("utf-8")).hexdigest(), 16)         % 999_999_999 + 1


def stream_format(
    fmt: EdiFormat,
    records: Iterable[dict[str, Any]],
    *,
    sender_id: str = "SDMSENDER",
    receiver_id: str = "RECEIVER",
    as_of: str | date = "2026-06-30",
    control_number: int | None = None,
    per_transaction: int | None = None,
    rules: dict[str, dict] | None = None,
    seed: str = "edi",
) -> Iterator[str]:
    """The interchange, a block of segments at a time.

    Records are keyed by element -- `MEMBER_ID`, `DOB`, `CLAIM_ID` -- never by
    a column. Where a record holds an element, that value is written; where it
    does not, the element's rule makes one.

    `per_transaction` is how many groups go in one ST/SE set -- members in an
    834, claims in an 837. One transaction holding a whole book is valid and
    nobody sends it: trading partners cap transaction size, and a file that
    has to be resent because one member in forty thousand was wrong is the
    reason they do. Left unset, everything goes in one.

    The interchange is one ISA/IEA whatever the count. Splitting into separate
    *files* is the caller's business; splitting into transactions is the
    format's.

    Never the clock: the date in the envelope comes from `as_of`, so the same
    data and the same date produce the same file on any machine and any day.
    """
    if rules is None:
        from sdg.analyzer.services.layout_service import rules_by_element

        rules = rules_by_element()

    stamp = str(as_of)[:10].replace("-", "")
    short_date, long_date = stamp[2:], stamp
    time_str = "0000"
    if control_number is None:
        control_number = interchange_number(seed)
    ctrl = f"{control_number:09d}"

    yield SEGMENT_TERM.join([
        ELEMENT_SEP.join([
            "ISA", "00", " " * 10, "00", " " * 10,
            "ZZ", sender_id[:15].ljust(15), "ZZ", receiver_id[:15].ljust(15),
            short_date, time_str, "^", "00501", ctrl, "0", usage_indicator(), ":",
        ]),
        ELEMENT_SEP.join([
            "GS", fmt.functional_code, sender_id, receiver_id, long_date,
            time_str, str(control_number), "X", fmt.version,
        ]),
    ]) + SEGMENT_TERM

    envelope = {
        "_control": ctrl,
        "_date": long_date,
        "_sender": sender_id,
        "_reference": f"{short_date}{control_number:02d}",
    }

    # Counted across the interchange, not within a transaction: a value made
    # from a rule is drawn at its group's position, and restarting that at
    # each ST would write the same member ID into every transaction.
    position = {"group": 0, "record": 0}
    transactions = 0
    if fmt.adjudication is not None:
        from sdg.writers.adjudication import adjudicated

        records = adjudicated(fmt, records, rules, seed=seed, as_of=str(as_of))
    for chunk in _transactions(fmt, records, per_transaction):
        transactions += 1
        yield from _one_transaction(
            fmt, chunk, envelope, transactions, rules, seed, str(as_of), position,
        )

    if transactions == 0:
        # An interchange with no transaction in it is not a file anybody can
        # read. Saying so beats sending an envelope with nothing inside.
        raise ValueError(
            f"no records to write as {fmt.id}; generate some first"
        )

    yield SEGMENT_TERM.join([
        ELEMENT_SEP.join(["GE", str(transactions), str(control_number)]),
        ELEMENT_SEP.join(["IEA", "1", ctrl]),
    ]) + SEGMENT_TERM


def _opens_on(fmt: EdiFormat) -> str | None:
    """The element whose change opens a group: a new claim, a new member."""
    found = next((g.get("opens_on") for g in fmt.groups if g.get("opens_on")), None)
    return str(found).strip().upper() if found else None


def _grouped(fmt: EdiFormat, records) -> Iterator[list[dict[str, Any]]]:
    """Records in their groups.

    A record without the element its group opens on is a group of its own,
    which is what a file made from rules alone looks like: three members asked
    for, three INS loops.
    """
    key = _opens_on(fmt)
    current: list[dict[str, Any]] = []
    marker: Any = object()
    for index, record in enumerate(records):
        value = record.get(key) if key and record.get(key) not in (None, "") else ("#", index)
        if current and value != marker:
            yield current
            current = []
        marker = value
        current.append(record)
    if current:
        yield current


def _transactions(fmt: EdiFormat, records, per_transaction: int | None):
    """Groups in chunks, split only between groups.

    A transaction may not end halfway through a claim: its lines belong to the
    CLM that opened them, and a chunk boundary inside one would put the rest in
    a different ST/SE set pointing at a claim that is not there.
    """
    chunk: list[list[dict[str, Any]]] = []
    for group in _grouped(fmt, records):
        if per_transaction and per_transaction > 0 and len(chunk) >= per_transaction:
            yield chunk
            chunk = []
        chunk.append(group)
    if chunk:
        yield chunk


def _one_transaction(
    fmt: EdiFormat, groups, envelope, number: int, rules, seed: str, as_of: str,
    position: dict[str, int],
) -> Iterator[str]:
    """One ST/SE set, counting its own segments as X12 requires."""
    from sdg.writers.edi_formats.resolve import segment_text

    control = f"{number:04d}"
    # Counters belong to the transaction: HL numbering starts again in each.
    context: dict[str, Any] = {**envelope, "_counters": {}}

    # The header's counters number first -- the billing provider is HL 1 --
    # so it claims them before the groups do, and its totals are filled in
    # once the groups exist.
    wants_totals = [
        (e["total"]["segment"], int(e["total"]["position"]))
        for spec in fmt.header for e in spec.get("elements") or [] if e.get("total")
    ]
    header_counters_seen = {
        e["counter"] for spec in fmt.header for e in spec.get("elements") or [] if e.get("counter")
    }
    if header_counters_seen:
        for spec in fmt.header:
            segment_text(spec, {}, rules, seed=seed, row_index=0, as_of=as_of,
                         context=context)
        claimed = dict(context["_counters"])
    else:
        claimed = {}

    segments: list[str] = []
    for group in groups:
        position["group"] += 1
        context["_group"] = group
        # Where this group's records will be drawn, so a total over them adds
        # up the same values the lines are about to be written with.
        context["_group_start"] = position["record"] + 1
        # Which loops this group shares with the one before it in this
        # transaction -- a subscriber's second claim under their first's HL.
        context["_shared"] = {}
        for spec_group in fmt.groups:
            for name in spec_group.get("resets") or []:
                context["_counters"].pop(name, None)
            for spec in spec_group.get("segments") or []:
                built = segment_text(
                    spec, group[0], rules, seed=seed,
                    row_index=position["group"], as_of=as_of, context=context,
                )
                if built:
                    segments.append(built)
        context["_previous"] = group[0]

        for record in group:
            position["record"] += 1
            for spec in fmt.per_record:
                built = segment_text(
                    spec, record, rules, seed=seed,
                    row_index=position["record"], as_of=as_of, context=context,
                )
                if built:
                    segments.append(built)

    totals: dict[tuple[str, int], float] = {}
    for segment_id, at in wants_totals:
        amount = 0.0
        for text in segments:
            parts = text.split(ELEMENT_SEP)
            if parts[0] == segment_id and len(parts) > at:
                try:
                    amount += float(parts[at])
                except ValueError:
                    continue
        totals[(segment_id, at)] = amount
    header_context = {**context, "_counters": dict(claimed), "_totals": totals}
    header: list[str] = []
    for spec in fmt.header:
        built = segment_text(
            spec, {}, rules, seed=seed, row_index=0, as_of=as_of, context=header_context,
        )
        if built:
            header.append(built)

    segments = [ELEMENT_SEP.join(["ST", fmt.transaction, control, fmt.version]),
                *header, *segments]
    segments.append(ELEMENT_SEP.join(["SE", str(len(segments) + 1), control]))

    block: list[str] = []
    for segment in segments:
        block.append(segment + SEGMENT_TERM)
        if len(block) >= BLOCK:
            yield "".join(block)
            block = []
    if block:
        yield "".join(block)


def _loops_v1(previous, record, keys) -> bool:
    """Version 1 of whether a group shares a loop with the one before it:
    never -- every claim under its own subscriber HL."""
    return False


def _loops_v2(previous, record, keys) -> bool:
    """Version 2: when the group before holds the same values of `keys`, and
    the record holds the last of them -- the same subscriber, the same
    patient. A value no record holds is drawn per claim, so it is never the
    same person."""
    if previous is None or record.get(keys[-1]) in (None, ""):
        return False
    return all(previous.get(k) == record.get(k) for k in keys)


def _register() -> None:
    from sdg.engine.generators import register_algorithm

    register_algorithm("shared loops", 1, _loops_v1)
    register_algorithm("shared loops", 2, _loops_v2)


_register()
