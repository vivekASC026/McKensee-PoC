"""HL7 v2 read and written for files modeled on a sample: the syntax, never a message type.

The sample is one HL7 v2 message (or a batch of them, FHS/BHS ... BTS/FTS).
The file written repeats its messages per record, everything the sample has
except the fields a generation config names. Segments, fields, components
and the config are shared with X12 (`segments.py`): the masking tool's
HL7v251Config.json uses the same `segment_id` / `position` (the segment id
counting as 1) / `component`, so it is a master as it is. What a message
type repeats and what a field holds are in its profile (`profiles/hl7v2*.json`).

What is HL7's own: delimiters from MSH-1 and MSH-2, a field's repetitions
(`~`; the first takes the record's value, any other its rule's), MSH counted
from its field separator (MSH-1), the batch envelope with BTS-1 counting the
messages written, and labels written PID-5.1.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from sdg.writers.samples import engine, segments
from sdg.writers.samples import profile as profiles
from sdg.writers.samples.base import SampleError
from sdg.writers.snip.x12 import Segment

FORMAT = "hl7v2"
LABEL = "HL7 v2"
MEDIA = "application/hl7-v2"
EXTENSION = ".hl7"
VALIDATION = "hl7v2"

ENVELOPE = frozenset({"FHS", "BHS", "BTS", "FTS"})

read_config = segments.read_config
write_config = segments.write_config
positions = segments.positions
used = segments.used


class Syntax(segments.Syntax):
    envelope = ENVELOPE
    offsets = {"MSH": 1}

    def label(self, kind: tuple, n: int, c: int) -> str:
        seg = kind[0]
        return f"{seg}-{n + self.offsets.get(seg, 0)}" + (f".{c}" if c else "")


SYNTAX = Syntax()


@dataclass
class Sample:
    text: str
    element_sep: str
    component_sep: str
    repetition_sep: str
    terminator: str
    newline: str
    head: list[Segment]          # FHS, BHS
    tail: list[Segment]          # BTS, FTS
    body: list[Segment]          # every message's segments
    kind: str                    # MSH-9.1: ADT, DFT, ORU ...
    version: str                 # MSH-12
    profile: dict[str, Any]
    anchors: list[dict[str, Any]]


def detect(text: str) -> bool:
    head = text.lstrip("﻿ \r\n\t")
    return head[:3] in ("MSH", "FHS", "BHS") and len(head) > 3 and not head[3].isalnum()


def subject(kind: str) -> str:
    return (profiles.for_kind(FORMAT, kind) or {}).get("subject", "members")


def facts(sample: Sample) -> tuple[dict[str, int], str]:
    counts = {name: sum(1 for s in sample.body if SYNTAX.selector(sel)(s))
              for name, sel in (sample.profile.get("counts") or {}).items()}
    counts["segments"] = len(sample.body)
    return counts, str(sample.profile.get("summary") or "").format(**counts)


def read_sample(text: str) -> Sample:
    body_text = text.lstrip("﻿ \r\n\t")
    if not detect(body_text):
        raise SampleError("This is not an HL7 v2 message: it does not begin with MSH (or FHS/BHS).")
    field_sep = body_text[3]
    for terminator in ("\r\n", "\n", "\r"):
        if terminator in body_text:
            break
    else:
        terminator = "\r"
    lines = [line for line in body_text.replace("\r\n", "\r").replace("\n", "\r").split("\r")
             if line.strip()]
    msh = next((line for line in lines if line.startswith("MSH")), None)
    if msh is None:
        raise SampleError("The sample has no MSH segment: there is no message in it.")
    encoding = msh.split(field_sep)[1] if len(msh.split(field_sep)) > 1 else "^~\\&"
    component_sep = encoding[0] if len(encoding) > 0 else "^"
    repetition_sep = encoding[1] if len(encoding) > 1 else "~"
    parsed = [Segment(index=i + 1, id=line.split(field_sep)[0], elements=line.split(field_sep)[1:],
                      raw=line) for i, line in enumerate(lines)]
    first = next(s for s in parsed if s.id == "MSH")
    kind = first.get(8).split(component_sep)[0]           # MSH-9.1
    prof = profiles.for_kind(FORMAT, kind)
    if prof is None:
        known = ", ".join(profiles.kinds(FORMAT))
        raise SampleError(f"The sample is an HL7 {kind or 'unknown'} message; a file can be "
                          f"modeled on {known} messages.")
    head = [s for s in parsed if s.id in ("FHS", "BHS")]
    tail = [s for s in parsed if s.id in ("BTS", "FTS")]
    body = [s for s in parsed if s.id not in ENVELOPE]
    return Sample(text=text, element_sep=field_sep, component_sep=component_sep,
                  repetition_sep=repetition_sep, terminator=terminator, newline="",
                  head=head, tail=tail, body=body, kind=kind, version=first.get(11),
                  profile=prof, anchors=segments.anchors(body, prof))


def propose(sample: Sample, entries, algorithms: Iterable[str]) -> list[dict[str, Any]]:
    return segments.propose(SYNTAX, sample, entries, algorithms)


class Reader(segments.Reader):
    syntax = SYNTAX
    # Segments and fields can be optional or repeat (NK1, OBX, PID-13).
    varies = True

    def __init__(self, sample: Sample):
        super().__init__(sample)
        self.messages = 0

    def codes(self, prof: dict[str, Any]) -> dict:
        out = {}
        for c in (prof.get("codes") or {}).get("list") or []:
            a = segments.at(c["at"])
            out[(a.segment, SYNTAX.element(a))] = (c["element"], c["map"], c.get("default"))
        return out

    def lines(self, top: engine.Written) -> list[str]:
        out = super().lines(top)
        self.messages += sum(1 for line in out if line.startswith("MSH"))
        return out

    def envelope(self, sample: Sample) -> tuple[list[str], list[str]]:
        return [s.raw for s in sample.head], []

    def closing(self, sample: Sample, count: int) -> list[str]:
        return []

    def after(self, sample: Sample) -> list[str]:
        """The batch trailer, BTS-1 counting the messages written."""
        out = []
        for s in sample.tail:
            if s.id == "BTS" and s.get(1).isdigit():
                out.append(sample.element_sep.join(["BTS", str(self.messages), *s.elements[1:]]))
            else:
                out.append(s.raw)
        return out


def stream(
    sample: Sample,
    entries,
    records: Iterable[dict[str, Any]],
    rules: dict[str, dict],
    *,
    seed: str = "hl7",
    as_of: str = "2026-06-30",
    vary: Iterable[engine.Vary] = (),
) -> Iterator[str]:
    """The sample's messages, repeated for these records as its profile says."""
    yield from engine.stream(Reader(sample), sample, entries, records, rules,
                             seed=seed, as_of=as_of, vary=vary)


# Segments a message is built on, never optional: the header, the patient,
# the visit, and what a profile's units start on.
_FIXED = {"MSH", "BHS", "BTS", "FHS", "FTS", "EVN", "PID"}


def structure(sample: Sample) -> list[dict[str, Any]]:
    """The places a config can vary: each segment of a message that is not
    what it is built on (NK1, AL1, OBX, IN1), with the most times a message
    has it in a row, and each field the sample fills, with its repetitions
    (PID-13 holding two phone numbers)."""
    fixed = set(_FIXED)

    def starts(specs) -> None:
        for spec in specs or []:
            fixed.update(str(spec.get("starts") or "").split("|"))
            starts(spec.get("units"))

    starts(sample.profile.get("units"))
    segs: dict[str, int] = {}
    fields: dict[str, int] = {}
    run_id, run = None, 0
    for seg in sample.body:
        run = run + 1 if seg.id == run_id else 1
        run_id = seg.id
        if seg.id not in fixed:
            segs[seg.id] = max(segs.get(seg.id, 0), run)
        if seg.id in ("MSH", "BHS", "BTS", "FHS", "FTS"):
            continue
        for n, value in enumerate(seg.elements, start=1):
            if value.strip():
                reps = len(value.split(sample.repetition_sep)) if sample.repetition_sep else 1
                place = f"{seg.id}-{n}"
                fields[place] = max(fields.get(place, 0), reps)
    return ([{"at": k, "count": n, "repeats": True} for k, n in segs.items()]
            + [{"at": k, "count": n, "repeats": True} for k, n in fields.items()])


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

DISCLAIMER = ("Each message is checked for HL7 v2 structure (segments, delimiters, the "
              "segments and fields the profile requires, the batch counts), unique control "
              "ids, dates written as HL7 dates and coded fields in the HL7 tables the profile "
              "lists. Not a conformance test against a message profile or an interface's own "
              "rules.")

# An HL7 v2 date/time (DTM): a year, then month, day, hours, minutes, seconds
# (with a fraction) and a zone, each optional after the one before it.
_DTM = re.compile(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2}(?:\.\d{1,4})?)?([+-]\d{4})?")


def _level(level: int, name: str, rules: int, findings: list[dict], notes=()) -> dict[str, Any]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    return {"level": level, "name": name, "rules": rules, "errors": errors,
            "warnings": warnings, "notes": list(notes), "findings": findings,
            "status": "fail" if errors else ("warn" if warnings else "pass")}


def _finding(level: int, rule: str, message: str, where: str,
             severity: str = "error") -> dict[str, Any]:
    return {"level": level, "rule": rule, "message": message, "raw": where, "severity": severity}


def _date(value: str) -> bool:
    m = _DTM.fullmatch(value)
    if m is None:
        return False
    year, month, day, hour, minute, second = m.group(1, 2, 3, 4, 5, 6)
    if month is not None and not 1 <= int(month) <= 12:
        return False
    if day is not None and not 1 <= int(day) <= calendar.monthrange(int(year), int(month))[1]:
        return False
    if hour is not None and int(hour) > 23:
        return False
    if minute is not None and int(minute) > 59:
        return False
    return second is None or int(second[:2]) <= 59


def _said(a: segments.At) -> str:
    return f"{a.segment}-{a.n}" + (f".{a.c}" if a.c else "")


def _values(seg: Segment, a: segments.At, component_sep: str, repetition_sep: str) -> list[str]:
    """What a profile place holds in a segment, each repetition's: its component,
    or the field's first component where the place names none."""
    field = seg.get(SYNTAX.element(a))
    out = []
    for rep in field.split(repetition_sep):
        parts = rep.split(component_sep)
        value = parts[(a.c or 1) - 1] if (a.c or 1) <= len(parts) else ""
        if value:
            out.append(value)
    return out


def check(text: str, prof: dict[str, Any]) -> dict[str, Any]:
    """A written file checked three ways: structure, identity, dates and codes."""
    checks = prof.get("checks") or {}
    fields = [segments.at(t) for t in checks.get("fields") or []]
    dates = [segments.at(t) for t in checks.get("dates") or []]
    code_rules = [(segments.at(r["at"]), r) for r in checks.get("codes") or []]
    required = list((prof.get("message") or {}).get("required") or ["MSH"])

    lines = [line for line in text.lstrip("﻿ \r\n\t").replace("\r\n", "\r")
             .replace("\n", "\r").split("\r") if line.strip()]
    if not lines or not detect(lines[0]):
        level = _level(1, "Structure", 1, [_finding(1, "begins", "The file does not begin with "
                                                    "MSH, FHS or BHS.", (lines or [""])[0][:60])])
        return {"passed": False, "summary": "Not an HL7 v2 file.", "levels": [level],
                "disclaimer": DISCLAIMER}
    field_sep = lines[0][3]
    msh = next((line for line in lines if line.startswith("MSH")), "")
    encoding = msh.split(field_sep)[1] if msh.count(field_sep) else "^~\\&"
    component_sep, repetition_sep = (encoding + "^~")[0], (encoding + "^~")[1]
    parsed = [Segment(index=i + 1, id=line.split(field_sep)[0],
                      elements=line.split(field_sep)[1:], raw=line)
              for i, line in enumerate(lines)]

    structure: list[dict] = []
    identity: list[dict] = []
    values: list[dict] = []

    # Messages: an MSH and the segments up to the next MSH or the batch trailer.
    messages: list[list[Segment]] = []
    for seg in parsed:
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2}", seg.id):
            structure.append(_finding(1, "segment-id", f"Segment {seg.index} has the id "
                                      f"{seg.id!r}, not three capitals or digits.", seg.raw[:60]))
        elif seg.id == "MSH":
            messages.append([seg])
            if seg.get(1) != encoding:
                structure.append(_finding(1, "delimiters", f"The MSH at segment {seg.index} has "
                                          "other encoding characters than the first message's.",
                                          seg.raw[:60]))
        elif seg.id in ENVELOPE:
            continue
        elif messages:
            messages[-1].append(seg)
        else:
            structure.append(_finding(1, "in-message", f"{seg.id} at segment {seg.index} comes "
                                      "before any MSH.", seg.raw[:60]))

    controls: dict[str, int] = {}
    for n, message in enumerate(messages, 1):
        head = message[0]
        control = head.get(SYNTAX.element(segments.at("MSH MSH10")))
        where = f"message {n} ({control or 'no control id'})"
        ids = {s.id for s in message}
        for seg_id in required:
            if seg_id not in ids:
                structure.append(_finding(1, "required-segment", f"No {seg_id} segment.", where))
        for a in fields:
            present = [s for s in message if s.id == a.segment]
            if present and not any(_values(s, a, component_sep, repetition_sep)
                                   for s in present):
                structure.append(_finding(1, "required-field", f"{_said(a)} is empty.", where))
        if control:
            if control in controls:
                identity.append(_finding(2, "control-id-unique", f"MSH-10 {control} is also "
                                         f"message {controls[control]}'s.", where))
            else:
                controls[control] = n
        for seg in message:
            for a in dates:
                if a.segment == seg.id:
                    for value in _values(seg, a, component_sep, repetition_sep):
                        if not _date(value):
                            values.append(_finding(3, "date", f"{_said(a)} is {value!r}, not an "
                                                   "HL7 date (YYYYMMDD[HHMM[SS]][+ZZZZ]).", where))
            for a, rule in code_rules:
                if a.segment == seg.id:
                    allowed = set(rule["in"])
                    for value in _values(seg, a, component_sep, repetition_sep):
                        if value not in allowed:
                            table = f"HL7 table {rule['table']}" if rule.get("table") else "its table"
                            values.append(_finding(3, f"code:{rule['at']}",
                                                   f"{_said(a)} is {value!r}, not in {table} "
                                                   f"({', '.join(sorted(allowed))}).", where))

    # A batch: BTS-1 counts its messages, FTS-1 the file's batches.
    batches = sum(1 for s in parsed if s.id == "BHS")
    for seg in parsed:
        if seg.id == "BTS" and seg.get(1) and seg.get(1) != str(len(messages)):
            structure.append(_finding(1, "batch-count", f"BTS-1 is {seg.get(1)}; the batch has "
                                      f"{len(messages)} messages.", seg.raw[:60]))
        if seg.id == "FTS" and seg.get(1) and seg.get(1) != str(batches):
            structure.append(_finding(1, "batch-count", f"FTS-1 is {seg.get(1)}; the file has "
                                      f"{batches} batches.", seg.raw[:60]))
    for opener, closer in (("FHS", "FTS"), ("BHS", "BTS")):
        if sum(s.id == opener for s in parsed) != sum(s.id == closer for s in parsed):
            structure.append(_finding(1, "batch-envelope", f"{opener} and {closer} do not pair.",
                                      opener))

    levels = [
        _level(1, "Structure: segments, delimiters, required segments and fields, batch counts",
               4 + len(required) + len(fields), structure),
        _level(2, "Message control ids (MSH-10) unique", 1, identity),
        _level(3, "Dates are HL7 dates; coded fields in their HL7 tables",
               len(dates) + len(code_rules), values),
    ]
    passed = not any(lv["errors"] for lv in levels)
    summary = f"{len(messages):,} messages; {'no errors' if passed else 'errors found'}."
    return {"passed": passed, "summary": summary, "levels": levels, "disclaimer": DISCLAIMER}
