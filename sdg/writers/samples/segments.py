"""What delimited segment syntaxes share: X12 and HL7 v2, read for the engine.

Both write a file as segments of fields of components, and the masking tool
addresses both the same way (`segment_id`, `qualifier`, `loop_segment_id`,
`loop_qualifier`, `position` counting the segment id as 1, `component`), so
the config language, the proposals and the filling are one piece of code; a
syntax module says only how its file is parsed and enveloped (`x12.py`,
`hl7v2.py`). What a kind of file repeats and what a place holds are in its
profile.

A sample here has: body (segments: .id, .elements, .raw, .get(n)), anchors
(per segment, the open loop segments by id), component_sep, element_sep,
repetition_sep (or None), profile, kind, version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from sdg.writers.samples import engine
from sdg.writers.samples.base import SampleError, without_comments

ROOT_KEYS = ("genSegments", "maskSegments")
FIELD_KEYS = ("genFields", "maskFields")


# ---------------------------------------------------------------------------
# Places and selectors, as profiles write them
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class At:
    """'NM1*IL NM103': segment NM1 whose first element is IL, element 3 (component c, or 0)."""
    segment: str
    quals: tuple[str, ...]
    n: int
    c: int


@lru_cache(maxsize=None)
def at(text: str) -> At:
    try:
        left, right = text.split()
        segment, *quals = left.split("*")
        if not right.startswith(segment):
            raise ValueError
        m = re.fullmatch(r"(\d{2})(?:-(\d+))?", right[len(segment):])
        if m is None:
            raise ValueError
    except ValueError:
        raise SampleError(f"Profile place {text!r} is not written as 'NM1*IL NM103'.") from None
    return At(segment, tuple(quals), int(m.group(1)), int(m.group(2) or 0))


@lru_cache(maxsize=None)
def _selector(text: str, offsets: tuple[tuple[str, int], ...]):
    m = re.fullmatch(r"([A-Z0-9]+)(?:\[(\d+)=([^\]]*)\])?", text.strip())
    if m is None:
        raise SampleError(f"Profile selector {text!r} is not written as 'HL' or 'HL[3=22]'.")
    seg_id, n, value = m.group(1), m.group(2), m.group(3)
    if n is None:
        return lambda seg: seg.id == seg_id
    element = int(n) - dict(offsets).get(seg_id, 0)
    return lambda seg: seg.id == seg_id and seg.get(element) == value


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    segment: str
    qualifiers: tuple[tuple[int, str], ...]     # (element number, value)
    loop: tuple[str, tuple[tuple[int, str], ...]] | None
    fields: tuple[tuple[int, int, str], ...]    # (element number, component or 0, name)
    raw: dict = field(default_factory=dict, compare=False, hash=False)


def _split_id(text: str, sep: str) -> list[str]:
    cleaned = str(text or "").replace("\\", "").strip()
    if not cleaned:
        raise SampleError("A config entry has an empty segment_id.")
    return cleaned.split(sep)


def _quals(parts: list[str], qualifier: str | None) -> tuple[tuple[int, str], ...]:
    out = [(i, p.strip()) for i, p in enumerate(parts[1:], start=1)]
    if qualifier is not None and str(qualifier).strip():
        out.append((len(parts), str(qualifier).strip()))
    return tuple(out)


def read_config(body: dict | str) -> list[Entry]:
    """A generation config, or a masking config read as one."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            try:
                body = json.loads(without_comments(body))
            except ValueError as exc:
                raise SampleError(f"The config is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise SampleError("The config is not a JSON object.")
    root = next((k for k in ROOT_KEYS if k in body), None)
    if root is None:
        raise SampleError("The config has no genSegments (or maskSegments) list.")
    sep = str(body.get("segment_delimiter") or "*")
    if sep == "|":          # HL7 v2's field delimiter; a segment_id never holds one
        sep = "*"
    entries = []
    for index, item in enumerate(body.get(root) or []):
        where = f"{root}[{index}]"
        if not isinstance(item, dict):
            raise SampleError(f"{where} is not an object.")
        parts = _split_id(item.get("segment_id"), sep)
        loop = None
        if str(item.get("loop_segment_id") or "").strip():
            loop_parts = _split_id(item["loop_segment_id"], sep)
            loop = (loop_parts[0].upper(), _quals(loop_parts, item.get("loop_qualifier")))
        fields = []
        for f in next((item[k] for k in FIELD_KEYS if k in item), None) or []:
            position = int(f.get("position", 0))
            if position < 2:
                raise SampleError(f"{where}: position {position} is the segment id itself; "
                                  "positions start at 2.")
            name = str((f.get("algorithm_reference") or {}).get("name") or "").strip()
            fields.append((position - 1, int(f.get("component") or 0), name))
        entries.append(Entry(parts[0].upper(), _quals(parts, item.get("qualifier")), loop,
                             tuple(fields), item))
    return entries


def write_config(rows: Iterable[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    """Rows chosen on the screen as a genSegments config."""
    grouped: dict[tuple, dict] = {}
    for row in rows:
        name = str(row.get("algorithm") or "").strip()
        if not name:
            continue
        key = (row["segment_id"], row.get("qualifier"), row.get("loop_segment_id"),
               row.get("loop_qualifier"))
        entry = grouped.setdefault(key, {
            "segment_id": row["segment_id"], "qualifier": row.get("qualifier"),
            "loop_segment_id": row.get("loop_segment_id"),
            "loop_qualifier": row.get("loop_qualifier"), "genFields": [],
        })
        one = {"position": int(row["position"]), "algorithm_reference": {"name": name}}
        if row.get("component"):
            one["component"] = int(row["component"])
        entry["genFields"].append(one)
    return {**{k: v for k, v in meta.items() if v is not None},
            "segment_delimiter": "*", "genSegments": list(grouped.values())}


def positions(config: dict[str, Any]) -> int:
    """How many positions a saved config generates."""
    return sum(len(e.get("genFields") or []) for e in config.get("genSegments") or [])


def used(entries: list[Entry]) -> set[str]:
    """The algorithm names the entries refer to."""
    return {name for e in entries for _n, _c, name in e.fields}


# ---------------------------------------------------------------------------
# The sample, as segments
# ---------------------------------------------------------------------------

def anchors(body: list, prof: dict[str, Any]) -> list[dict[str, Any]]:
    """For each segment, the loop segment it sits under, by that segment's id."""
    loops = prof.get("loops") or {}
    ends, anchor_ids = set(loops.get("ends") or []), set(loops.get("anchors") or [])
    open_: dict[str, Any] = {}
    out = []
    for seg in body:
        if seg.id in ends:
            for key in list(open_):
                open_[key] = None
        if seg.id in anchor_ids:
            open_[seg.id] = seg
        out.append(dict(open_))
    return out


def _matches(seg, quals) -> bool:
    return all(seg.get(n).strip() == v for n, v in quals)


def entry_applies(entry: Entry, seg, anchors_: dict[str, Any]) -> bool:
    if seg.id != entry.segment or not _matches(seg, entry.qualifiers):
        return False
    if entry.loop is None:
        return True
    anchor = anchors_.get(entry.loop[0])
    return anchor is not None and anchor is not seg and _matches(anchor, entry.loop[1])


def _kind(seg, anchors_, prof: dict[str, Any]) -> tuple:
    """What a segment is, as a config entry would address it."""
    count = int((prof.get("qualified") or {}).get(seg.id, 0))
    quals = tuple(seg.get(n).strip() for n in range(1, count + 1))
    loops = prof.get("loops") or {}
    by = loops.get("by")
    loop = None
    if seg.id in (loops.get("scoped") or []) and anchors_.get(by) is not None:
        loop = (by, anchors_[by].get(1).strip())
    return seg.id, quals, loop


def _entry_text(kind: tuple) -> dict[str, Any]:
    """A kind as config fields: a second qualifier goes into the segment id (DTP*472)."""
    seg, quals, loop = kind
    if len(quals) == 2:
        segment_id, qualifier = f"{seg}\\*{quals[0]}", quals[1]
    else:
        segment_id, qualifier = seg, (quals[0] if quals else None)
    return {"segment_id": segment_id, "qualifier": qualifier,
            "loop_segment_id": loop[0] if loop else None,
            "loop_qualifier": loop[1] if loop else None}


# ---------------------------------------------------------------------------
# A syntax: what X12 and HL7 v2 say differently about the same segments
# ---------------------------------------------------------------------------

class Syntax:
    """Overridden by a syntax module: envelope segments, field numbering, labels."""

    envelope: frozenset[str] = frozenset()
    # Segments whose fields the syntax numbers one higher than their place
    # after the id (HL7's MSH, whose MSH-1 is the field separator itself).
    offsets: dict[str, int] = {}

    def skipped(self, prof: dict[str, Any]) -> set[str]:
        """Segments whose values the envelope or the structure carries."""
        return set(self.envelope) | set((prof.get("structural") or {}).get("segments") or [])

    def element(self, a: At) -> int:
        """A profile place's element number in the parsed segment."""
        return a.n - self.offsets.get(a.segment, 0)

    def selector(self, text: str):
        return _selector(text, tuple(sorted(self.offsets.items())))

    def label(self, kind: tuple, n: int, c: int) -> str:
        seg, quals, loop = kind
        text = "*".join([seg, *quals])
        if loop:
            text += f" (in {loop[0]}*{loop[1]})"
        return f"{text} {seg}{n + self.offsets.get(seg, 0):02d}" + (f"-{c}" if c else "")


# ---------------------------------------------------------------------------
# Proposing an algorithm for every position of the sample
# ---------------------------------------------------------------------------

def propose(syntax: Syntax, sample, entries: list[Entry],
            algorithms: Iterable[str]) -> list[dict[str, Any]]:
    """Every position that holds a value in the sample, and what the config says for it.

    `algorithm` is the proposal: the config's name where it is an SDM algorithm,
    else the one the position's place implies. A position the config does not
    name is copied from the sample (`algorithm` None), with `suggested` saying
    what it could be.
    """
    prof = sample.profile
    known = {a.upper() for a in algorithms}
    skipped = syntax.skipped(prof)
    derived = [at(t) for t in engine.derived_targets(prof)]
    kinds: dict[tuple, dict[str, Any]] = {}
    for seg, anchors_ in zip(sample.body, sample.anchors):
        if seg.id in skipped:
            continue
        kind = _kind(seg, anchors_, prof)
        slot = kinds.setdefault(kind, {"count": 0, "values": {}, "entries": {}, "first": seg})
        slot["count"] += 1
        for n, value in enumerate(seg.elements, start=1):
            # What a derive writes is not a place; one of a component (DG1-3.2,
            # a code's text) leaves the element's other components places.
            made = {d.c for d in derived
                    if d.segment == seg.id and syntax.element(d) == n and
                    all(seg.get(i).strip() == q for i, q in enumerate(d.quals, 1))}
            if not value or 0 in made:
                continue
            first = value.split(sample.repetition_sep)[0] if sample.repetition_sep else value
            pieces = first.split(sample.component_sep)
            comps = [(0, first)] if len(pieces) == 1 else list(enumerate(pieces, start=1))
            for c, piece in comps:
                if not piece or c in made:
                    continue
                slot["values"].setdefault((n, c), piece)
                if (n, c) not in slot["entries"]:
                    for entry in entries:
                        if entry_applies(entry, seg, anchors_):
                            for en, ec, name in entry.fields:
                                if en == n and (ec == c or (ec == 0 and c == 0)):
                                    slot["entries"][(n, c)] = name
                                    break
                        if (n, c) in slot["entries"]:
                            break
                    # A whole-element entry over a composite covers its components.
                    if c and (n, c) not in slot["entries"]:
                        for entry in entries:
                            if entry_applies(entry, seg, anchors_):
                                hit = next((name for en, ec, name in entry.fields
                                            if en == n and ec == 0), None)
                                if hit:
                                    slot["entries"][(n, 0)] = hit

    places, overrides = _places(syntax, prof)
    rows = []
    for kind, slot in kinds.items():
        for (n, c), example in sorted(slot["values"].items()):
            config_name = slot["entries"].get((n, c)) or (
                slot["entries"].get((n, 0)) if c else None)
            place, blocked = _place(prof, places, kind, n, c)
            for o, condition, element in overrides:
                if (o.segment == kind[0] and kind[1][:len(o.quals)] == o.quals
                        and syntax.element(o) == n
                        and all(slot["first"].get(k) == v for k, v in condition)):
                    place = element
                    break
            algorithm, reason = engine.decide(config_name, place, blocked, known)
            rows.append({
                **_entry_text(kind),
                "position": n + 1, "component": c or None,
                "label": syntax.label(kind, n, c), "example": example, "count": slot["count"],
                "config_name": config_name, "algorithm": algorithm, "reason": reason,
                "suggested": place if place in known else None,
                "kept": reason.startswith(engine.KEPT),
            })
    return rows


def _places(syntax: Syntax, prof: dict[str, Any]):
    """The profile's places: by address, and those that hold only when a condition does."""
    places: dict[tuple, str | None] = {}
    overrides = []
    for p in (prof.get("places") or {}).get("list") or []:
        a = at(p["at"])
        if p.get("if"):
            condition = tuple((syntax.element(at(f"{a.segment} {k}")), v)
                              for k, v in p["if"].items())
            overrides.append((a, condition, p["is"]))
        else:
            places[(a.segment, a.quals, syntax.element(a), a.c)] = p["is"]
    return places, overrides


def _place(prof: dict[str, Any], places, kind: tuple, n: int, c: int) -> tuple[str | None, bool]:
    """The element a position's place implies, and whether it implies none."""
    seg, quals, loop = kind
    loops = prof.get("loops") or {}
    member = prof.get("member") or {}
    member_loops = set(member.get("loops") or [])
    if seg in (loops.get("scoped") or []) and (loop is None or loop[1] not in member_loops):
        return None, True
    if seg == loops.get("by") and quals[:1] and quals[0] not in member_loops:
        return None, True
    for key in ((seg, quals, n, c), (seg, quals[:1], n, c), (seg, (), n, c)):
        if key in places:
            return places[key], places[key] is None
    # A syntax whose member is a set of segments (HL7's PID, IN1): elsewhere
    # (a doctor in PV1) a masking name says nothing about the member.
    if member.get("segments") and seg not in member["segments"]:
        return None, True
    return None, False


# ---------------------------------------------------------------------------
# Writing: the reader the engine is given
# ---------------------------------------------------------------------------

def _vary_segment(where: str) -> str:
    """The segment a varied place is in: 'NK1', 'PID-13' -> 'NK1', 'PID'."""
    return where.strip().split("-", 1)[0].split(" ", 1)[0].upper()


def _vary_field(where: str) -> int | None:
    """The field a varied place is, or None for the whole segment: 'PID-13' -> 13."""
    text = where.strip()
    if "-" not in text:
        return None
    number = text.split("-", 1)[1].strip()
    return int(number) if number.isdigit() and int(number) > 0 else None


@dataclass
class Tpl:
    seg: Any
    fills: list[tuple[int, int, str]]
    sample: Any
    codes: dict


@dataclass
class Item:
    """A segment as written: its id, its elements, and the template it came from.

    A segment a config varies (`vary` at its id, as NK1) is written `times`
    times: none where it is left out; the copies past the first are filled by
    the rules, as a second identifier is, and numbered on from its set ID."""
    seg_id: str
    parts: list[str]
    tpl: Tpl
    times: int = 1
    copies: list[list[str]] | None = None


class Reader:
    """Segments for the engine: one per file written. A syntax subclasses it
    for its codes and envelope."""

    syntax: Syntax

    def __init__(self, sample):
        self.sample = sample

    def codes(self, prof: dict[str, Any]) -> dict:
        return {}

    def shape(self, sample, entries: list[Entry], known: set[str]) -> engine.Shape:
        prof = sample.profile
        codes = self.codes(prof)
        skipped = self.syntax.skipped(prof)
        tpls = []
        for seg, anchors_ in zip(sample.body, sample.anchors):
            fills: list[tuple[int, int, str]] = []
            if seg.id not in skipped:
                for entry in entries:
                    if entry_applies(entry, seg, anchors_):
                        for n, c, name in entry.fields:
                            if name.upper() in known and not any(
                                    f[0] == n and f[1] == c for f in fills):
                                fills.append((n, c, name.upper()))
            tpls.append(Tpl(seg, fills, sample, codes))
        inner = [t for t in tpls if t.seg.id not in self.syntax.envelope]
        units = prof.get("units") or []
        if not units:
            raise SampleError(f"Profile {prof['name']} names no unit to repeat.")
        head, occurrences = self._level(inner, units[0])
        if not occurrences:
            raise SampleError(units[0].get("missing") or
                              f"The sample has no {units[0]['name']} to model on.")
        return engine.Shape(head, occurrences, units[0])

    def _level(self, tpls: list[Tpl], spec: dict[str, Any]):
        starts = self.syntax.selector(spec["starts"])
        stops = self.syntax.selector(spec["stops"]) if spec.get("stops") else None
        head, occurrences = engine.cut(tpls, spec, lambda t: starts(t.seg),
                                       (lambda t: stops(t.seg)) if stops else None)
        child = (spec.get("units") or [None])[0]
        out = []
        for occ in occurrences:
            if child is None:
                out.append(engine.Unit(spec, occ))
                continue
            prefix, kids = self._level(occ, child)
            after: list[Tpl] = []
            if child.get("while") is not None and kids:
                # The last child holds its start and the segments named in
                # `while`; from the first other one on, the segments are the
                # unit's again, written after its children (a DFT's DG1, IN1).
                keep = set(child["while"])
                last = kids[-1].items
                cut_at = next((i for i, t in enumerate(last[1:], start=1)
                               if t.seg.id not in keep), len(last))
                kids[-1].items, after = last[:cut_at], last[cut_at:]
            if spec.get("requires") and not kids:
                continue
            out.append(engine.Unit(spec, prefix, kids, after))
        return head, out

    def fill(self, tpl: Tpl, record: dict[str, Any], row: int, values) -> Item:
        varies = getattr(values, "vary", ()) if self.varies else ()
        mine = [v for v in varies if _vary_segment(v.at) == tpl.seg.id]
        times = 1
        for vary in mine:
            if _vary_field(vary.at) is None:
                times = values.times(vary, row, self._place(tpl), 1)
        item = self._filled(tpl, record, row, values,
                            [v for v in mine if _vary_field(v.at) is not None])
        item.times = times
        if times > 1:
            # Copies past the sample's: the same segment, filled by the rules
            # at a row of their own, numbered on from its set ID.
            item.copies = []
            for k in range(1, times):
                copy = self._filled(tpl, {}, row * 100 + k, values, []).parts
                if copy and copy[0].strip().isdigit():
                    copy[0] = str(int(copy[0]) + k)
                item.copies.append(copy)
        return item

    # Whether this syntax writes what a config varies (HL7 v2 does; X12 keeps
    # its loops to the records).
    varies = False

    def _place(self, tpl: Tpl) -> int:
        """Tells apart the sample's segments of one id (a second NK1)."""
        return next((i for i, s in enumerate(self.sample.body) if s is tpl.seg), 0)

    def _filled(self, tpl: Tpl, record: dict[str, Any], row: int, values,
                fields: list[Any]) -> Item:
        s = tpl.sample
        parts = list(tpl.seg.elements)
        for vary in fields:
            # A field left out, or repeated as many times as drawn: a
            # repetition past the sample's takes the sample's in turn.
            n = _vary_field(vary.at)
            while len(parts) < n:
                parts.append("")
            reps = parts[n - 1].split(s.repetition_sep) if s.repetition_sep else [parts[n - 1]]
            if not parts[n - 1]:
                continue
            times = values.times(vary, row, self._place(tpl) * 100 + n, len(reps))
            parts[n - 1] = (s.repetition_sep or "~").join(reps[i % len(reps)]
                                                          for i in range(times))
        for n, c, name in tpl.fills:
            while len(parts) < n:
                parts.append("")
            current = parts[n - 1]
            reps = current.split(s.repetition_sep) if s.repetition_sep else [current]
            for r, rep in enumerate(reps):
                # One record holds one value per element: the first repetition takes
                # it, any other (a second identifier) the rule's.
                made = values(name, record, row) if r == 0 else values(name, {}, row * 100 + r)
                if not made:
                    continue
                if c:
                    comps = rep.split(s.component_sep)
                    while len(comps) < c:
                        comps.append("")
                    comps[c - 1] = engine.fit(made, comps[c - 1])
                    reps[r] = s.component_sep.join(comps)
                else:
                    reps[r] = engine.fit(self._code(tpl, n, name, made), rep)
            parts[n - 1] = s.repetition_sep.join(reps) if s.repetition_sep else reps[0]
        # Positions that follow from a filled element without being named --
        # INS01 is Y exactly when INS02 says the member is the subscriber.
        for (seg_id, n), (element, mapping, default) in tpl.codes.items():
            if seg_id == tpl.seg.id and n <= len(parts) and parts[n - 1] and not any(
                    f[0] == n for f in tpl.fills) and any(f[2] == element for f in tpl.fills):
                made = values(element, record, row)
                if made:
                    parts[n - 1] = str(mapping.get(made.strip(), default or parts[n - 1]))
        return Item(tpl.seg.id, parts, tpl)

    @staticmethod
    def _code(tpl: Tpl, n: int, element: str, value: str) -> str:
        """The value as the syntax codes it at this position, where it has codes for it."""
        known = tpl.codes.get((tpl.seg.id, n))
        if not known or known[0] != element:
            return value
        return str(known[1].get(value.strip(), known[2] if known[2] is not None else value))

    def render(self, item: Item) -> str:
        return item.tpl.sample.element_sep.join([item.seg_id, *item.parts])

    def lines(self, top: engine.Written) -> list[str]:
        """A unit's segments: its own, its children's, then its own again, as they
        sit -- a varied one as many times as it was drawn."""
        out = []
        for item in top.walk():
            if item.times == 0:
                continue
            out.append(self.render(item))
            for copy in item.copies or []:
                out.append(item.tpl.sample.element_sep.join([item.seg_id, *copy]))
        return out

    def _place_of(self, item: Item, where: str) -> tuple[At, int]:
        a = at(where)
        return a, self.syntax.element(a)

    def matches(self, item: Item, where: str) -> bool:
        a, _n = self._place_of(item, where)
        return item.seg_id == a.segment and all(
            (item.parts[i - 1] if i <= len(item.parts) else "").strip() == q
            for i, q in enumerate(a.quals, start=1))

    def get(self, item: Item, where: str) -> str | None:
        a, n = self._place_of(item, where)
        if n > len(item.parts):
            return None
        value = item.parts[n - 1]
        if not a.c:
            return value
        comps = value.split(item.tpl.sample.component_sep)
        return comps[a.c - 1] if a.c <= len(comps) else None

    def set(self, item: Item, where: str, value: str) -> None:
        a, n = self._place_of(item, where)
        while len(item.parts) < n:
            item.parts.append("")
        if not a.c:
            item.parts[n - 1] = value
            return
        sep = item.tpl.sample.component_sep
        comps = item.parts[n - 1].split(sep)
        while len(comps) < a.c:
            comps.append("")
        comps[a.c - 1] = value
        item.parts[n - 1] = sep.join(comps)

    def original(self, item: Item, where: str) -> str:
        a, n = self._place_of(item, where)
        value = item.tpl.seg.get(n)
        if not a.c:
            return value
        comps = value.split(item.tpl.sample.component_sep)
        return comps[a.c - 1] if a.c <= len(comps) else ""

    def drop(self, item: Item, where: str) -> None:
        self.set(item, where, "")

    def aliases(self, item: Item, old: str, new: str) -> dict[str, str]:
        return {}

    def relink(self, item: Item, mapping: dict[str, str]) -> None:
        return None

    def join(self, lines: list[str]) -> str:
        end = self.sample.terminator + self.sample.newline
        return "".join(line + end for line in lines)
