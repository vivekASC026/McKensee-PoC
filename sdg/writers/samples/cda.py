"""HL7 v3 XML read and written for files modeled on a sample: the syntax, never a kind of document.

HL7 v3 comes as documents (CDA R2: a C-CDA CCD) and as messages (a patient
registry add, PRPA_IN201301UV02); both are the same XML, so one reader writes
both and the profile says which: its `root` element, and what it `match`es
(a document's LOINC code, a message's interaction id). What follows is said
of documents; a message is written the same way, one per member.

A CDA is one ClinicalDocument about one patient, and a patient has any
number of them (one per encounter), all laid out alike. So every document
written is the sample's document, every element and attribute it has, except
the places a generation config names, filled by the same rules whichever and
however many there are. How many a member gets comes from the records (the
profile groups them: a document per claim, per member); which places are the
member's, the same in all of the member's documents, is the profile's
`levels`. Downloaded, the documents are files in a zip (`CCD-00001.xml`,
...), the way CDA documents travel; previewed and checked, they are the
documents one after another, each starting with its XML declaration. What repeats, what follows from what and
what a place holds are in the profile (`profiles/cda*.json`).

The config is the masking tool's CDA config (edi-mask `CDAR2Config.json`),
with `genPaths` for `maskPaths`: XPath-shaped paths from the document's root
(`recordTarget/patientRole/patient/name/family`), `//` for any depth, `@name`
for an attribute, every step matching every child of that name. A rule's
`valuePrefix` (`tel:`) says which values of a place it is for, and the prefix
stays on the value; `where` (written by this tool) tests the element's other
attributes (an id's `root`). A masking config, comments and all, can be read
as the master.

Where this module stops is XML's and HL7 v3's own grammar, the same for
every document: a URI value keeps its scheme, a timestamp keeps the
sample's precision, time of day and zone (`20141015103026-0500`), and a
code's `displayName` follows its `code`.
"""

from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Iterator
from lxml import etree

from sdg.writers.samples import engine
from sdg.writers.samples import profile as profiles
from sdg.writers.samples.base import SampleError, without_comments

FORMAT = "cda"
LABEL = "HL7 v3 XML (CDA documents, messages)"
MEDIA = "application/zip"
EXTENSION = ".zip"
# The documents unpacked, one after another: what a preview shows and the checks read.
TEXT_MEDIA = "application/xml"
VALIDATION = "cda"

NS = "urn:hl7-org:v3"
DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
PATH_KEYS = ("genPaths", "maskPaths")

# A value written as a URI: the scheme says what it is and stays on it.
_SCHEME = re.compile(r"(tel|fax|mailto|https?):", re.I)
# An HL7 v3 point in time: a day, then optionally a time, a fraction and a zone.
_TS = re.compile(r"\d{8}(\d{2,6}(\.\d+)?)?([+-]\d{4})?")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _pattern(path: str) -> re.Pattern:
    """A config path as a test of a place's full path: 'a/b' from the root,
    '//a/b' at any depth, '@x' an attribute."""
    text = path.strip()
    for root in ("/ClinicalDocument/", "ClinicalDocument/"):
        if text.startswith(root):
            text = text[len(root):]
    anywhere = text.startswith("//")
    steps = text.lstrip("/").split("/")
    out, deep = [], anywhere
    for i, step in enumerate(steps):
        if step == "":
            deep = True
            continue
        if not re.fullmatch(r"@?(?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*", step):
            raise SampleError(f"CDA path {path!r}: {step!r} is not an element or @attribute.")
        if step.startswith("@") and i != len(steps) - 1:
            raise SampleError(f"CDA path {path!r}: an attribute ends a path.")
        piece = re.escape(step)
        if out:
            out.append("/(?:.*/)?" if deep else "/")
        elif deep:
            out.append("(?:.*/)?")
        out.append(piece)
        deep = False
    if not out:
        raise SampleError(f"CDA path {path!r} names nothing.")
    return re.compile("".join(out))


def _reaches(path: str, place: str) -> bool:
    return _pattern(path).fullmatch(place) is not None


def _name(tag: str, prefixes: dict[str, str]) -> str:
    """A tag or attribute name as a path writes it: HL7's own unprefixed,
    another namespace's with the sample's prefix (sdtc:raceCode)."""
    if not tag.startswith("{"):
        return tag
    uri, local = tag[1:].split("}", 1)
    if uri == NS:
        return local
    return f"{prefixes.get(uri, uri)}:{local}"


@dataclass(frozen=True)
class Node:
    index: int            # in the document's iter() order, the same in every copy
    element: Any          # an lxml element
    attr: str | None      # the attribute's tag; None for the element itself
    path: str             # 'recordTarget/patientRole/id/@extension'


def _nodes(root: Any, prefixes: dict[str, str], base: str = "") -> Iterator[Node]:
    """Every element and attribute below the root, with its full path; the
    root's own path is `base` (an entry taken out of its document keeps the
    path it had there)."""
    stack: list[tuple[Any, str]] = [(root, base)]
    # lxml makes an element's Python object on demand: held here, each keeps its id.
    alive = list(root.iter())
    order = {id(e): i for i, e in enumerate(alive)}
    while stack:
        element, path = stack.pop()
        index = order[id(element)]
        if path:
            yield Node(index, element, None, path)
        for attr in element.attrib:
            name = "@" + _name(attr, prefixes)
            yield Node(index, element, attr, f"{path}/{name}" if path else name)
        children = [c for c in element if isinstance(c.tag, str)]
        for child in reversed(children):
            step = _name(child.tag, prefixes)
            stack.append((child, f"{path}/{step}" if path else step))


def _value(node: Node) -> str | None:
    """What a place holds: an attribute's value, or an element's own text."""
    if node.attr is not None:
        value = node.element.get(node.attr)
        return value if value is not None and value.strip() else None
    if any(isinstance(c.tag, str) for c in node.element):
        return None          # mixed content (a narrative): no single value
    text = node.element.text
    return text if text is not None and text.strip() else None


def _put(node_element: Any, attr: str | None, value: str) -> None:
    if attr is not None:
        node_element.set(attr, value)
    else:
        node_element.text = value


def _prefix(value: str) -> str | None:
    m = _SCHEME.match(value)
    return value[: m.end()] if m else None


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    path: str
    where: tuple[tuple[str, str], ...]
    prefix: str | None
    name: str


def read_config(body: dict | str) -> list[Entry]:
    """A generation config, or a masking config read as one."""
    if isinstance(body, str):
        try:
            body = json.loads(without_comments(body))
        except ValueError as exc:
            raise SampleError(f"The config is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise SampleError("The config is not a JSON object.")
    key = next((k for k in PATH_KEYS if k in body), None)
    if key is None:
        raise SampleError("The config has no genPaths (or maskPaths) list.")
    entries = []
    for rule in body.get(key) or []:
        name = str((rule.get("algorithm_reference") or {}).get("name") or "").strip()
        if not name or not rule.get("path"):
            continue
        _pattern(rule["path"])
        where = tuple(sorted((str(k), str(v)) for k, v in (rule.get("where") or {}).items()))
        entries.append(Entry(rule["path"], where, rule.get("valuePrefix") or None, name))
    return entries


def write_config(rows: Iterable[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    """Rows chosen on the screen as a genPaths config."""
    paths = []
    for row in rows:
        name = str(row.get("algorithm") or "").strip()
        if not name:
            continue
        one: dict[str, Any] = {"path": row["path"]}
        if row.get("where"):
            one["where"] = row["where"]
        if row.get("prefix"):
            one["valuePrefix"] = row["prefix"]
        one["algorithm_reference"] = {"name": name}
        paths.append(one)
    return {**{k: v for k, v in meta.items() if v is not None},
            "format": "HL7 CDA R2", "genPaths": paths}


def positions(config: dict[str, Any]) -> int:
    return len(config.get("genPaths") or [])


def used(entries: list[Entry]) -> set[str]:
    return {e.name for e in entries}


def _entry_for(entries: list[Entry], path: str, attrs: dict[str, str],
               prefix: str | None) -> Entry | None:
    """The rule for a place: one written for its prefix before one for any value."""
    fitting = [e for e in entries if _reaches(e.path, path)
               and all(attrs.get(k) == v for k, v in e.where)]
    return (next((e for e in fitting if e.prefix and e.prefix == prefix), None)
            or next((e for e in fitting if not e.prefix), None))


# ---------------------------------------------------------------------------
# The sample
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    text: str
    prolog: str                   # what comes before the root: the declaration, a stylesheet
    root: Any                     # lxml
    prefixes: dict[str, str]      # namespace -> the sample's prefix
    kind: str
    profile: dict[str, Any]
    version: str = "R2"


def detect(text: str) -> bool:
    head = text.lstrip("﻿ \r\n\t")[:4000]
    return head.startswith("<") and ("ClinicalDocument" in head or NS in head)


def _parse(text: str) -> tuple[str, Any, dict[str, str]]:
    """A document's prolog, root and namespace prefixes.

    The text arrives decoded, so it is parsed as the UTF-8 it is now, whatever
    its declaration said, and written under a UTF-8 declaration. Comments and
    processing instructions are not kept: they are the sample's own notes,
    which no config can replace, and would be copied into every document. A
    stylesheet before the root is kept, as how the document is shown."""
    text = text.lstrip("﻿")
    if re.search(r"<!(DOCTYPE|ENTITY)", text[:4000]):
        raise SampleError("The document declares a DOCTYPE or entities; a CDA has neither.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             encoding="utf-8")
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        raise SampleError(f"The document is not well-formed XML: {exc}") from None
    prefixes: dict[str, str] = {}
    for element in root.iter():
        for prefix, uri in (element.nsmap or {}).items() if isinstance(element.tag, str) else ():
            if prefix and uri != NS:
                prefixes.setdefault(uri, prefix)
    stylesheets = [etree.tostring(pi, encoding="unicode").strip()
                   for pi in reversed(list(root.itersiblings(preceding=True)))
                   if isinstance(pi, etree._ProcessingInstruction)
                   and pi.target == "xml-stylesheet"]
    _without_notes(root)
    prolog = DECLARATION + "\n" + "".join(pi + "\n" for pi in stylesheets)
    return prolog, root, prefixes


def _without_notes(root: Any) -> None:
    """The comments and processing instructions inside a document taken out,
    the whitespace around them left as the elements beside them had it."""
    for note in list(root.iter(etree.Comment, etree.ProcessingInstruction)):
        parent = note.getparent()
        if parent is None:
            continue
        before = note.getprevious()
        lead = before.tail if before is not None else parent.text
        tail = note.tail
        if not (lead or "").strip() and not (tail or "").strip():
            # Indentation only on both sides: the next element takes the
            # note's place, indented as the note's tail says.
            joined = tail if tail is not None else lead
        else:
            joined = (lead or "") + (tail or "")
        if before is not None:
            before.tail = joined
        else:
            parent.text = joined
        parent.remove(note)


def _match(root: Any, prefixes: dict[str, str], conditions: list[dict]) -> bool:
    for cond in conditions:
        wanted = set(cond.get("in") or [])
        if not any(_reaches(cond["at"], n.path) and _value(n) in wanted
                   for n in _nodes(root, prefixes)):
            return False
    return True


def _profile_for(root: Any, prefixes: dict[str, str]) -> dict[str, Any] | None:
    """The profile of this root element whose `match` the document satisfies most fully."""
    best, score = None, -1
    for kind in profiles.kinds(FORMAT):
        prof = profiles.for_kind(FORMAT, kind)
        need = prof.get("match") or []
        if prof.get("root", "ClinicalDocument") != _local(root):
            continue
        if _match(root, prefixes, need) and len(need) > score:
            best, score = prof, len(need)
    return best


def subject(kind: str) -> str:
    return (profiles.for_kind(FORMAT, kind) or {}).get("subject", "members")


def facts(sample: Sample) -> tuple[dict[str, int], str]:
    nodes = [n for n in _nodes(sample.root, sample.prefixes) if n.attr is None]
    counts = {name: sum(1 for n in nodes if _reaches(path, n.path))
              for name, path in (sample.profile.get("counts") or {}).items()}
    return counts, str(sample.profile.get("summary") or "").format(**counts)


def read_sample(text: str) -> Sample:
    prolog, root, prefixes = _parse(text)
    if not str(root.tag).startswith(f"{{{NS}}}"):
        raise SampleError("The sample is not HL7 v3 XML: its root is not in urn:hl7-org:v3.")
    prof = _profile_for(root, prefixes)
    if prof is None:
        raise SampleError(f"The {_local(root)} fits no HL7 v3 profile; one can be modeled "
                          f"on: {', '.join(profiles.kinds(FORMAT))}.")
    return Sample(text=text, prolog=prolog, root=root, prefixes=prefixes,
                  kind=prof["kind"], profile=prof, version=prof.get("version", "R2"))


# ---------------------------------------------------------------------------
# Proposing an algorithm for every place of the sample
# ---------------------------------------------------------------------------

def _qualifier(prof: dict[str, Any], node: Node) -> tuple[tuple[str, str], ...]:
    """The element's other attributes that say what a value is (an id's root)."""
    tail = "/".join(node.path.split("/")[-2:])
    keys = (prof.get("qualified") or {}).get(tail) or []
    return tuple((k, node.element.get(k)) for k in keys if node.element.get(k) is not None)


def _local(element: Any) -> str:
    tag = element.tag
    return tag.split("}", 1)[1] if isinstance(tag, str) and tag.startswith("{") else str(tag)


def _first(element: Any, rel: str, prefixes: dict[str, str]) -> str | None:
    """The value at a path relative to an element ('code/@code')."""
    return next((_value(n) for n in _nodes(element, prefixes) if n.path == rel), None)


def _within(prof: dict[str, Any], element: Any, prefixes: dict[str, str],
            inherited: dict[str, str] | None = None) -> dict[str, str]:
    """What the elements a place is inside say about it, by the profile's
    `within`: a place in a section is that section's (its LOINC code), so a
    date among the procedures is not a date among the allergies."""
    out: dict[str, str] = {}
    for name, rel in ((k, v) for k, v in (prof.get("within") or {}).items() if k != "about"):
        holder = next((a for a in element.iterancestors() if _local(a) == name), None)
        if holder is None and _local(element) == name:
            holder = element
        value = _first(holder, rel, prefixes) if holder is not None else (inherited or {}).get(name)
        if value is not None:
            out[name] = value
    return out


def _structural(prof: dict[str, Any], node: Node) -> bool:
    spec = prof.get("structural") or {}
    elements = set(spec.get("elements") or [])
    steps = node.path.split("/")
    if any(step in elements for step in steps):
        return True
    return node.attr is not None and steps[-1][1:] in set(spec.get("attributes") or [])


def _member(prof: dict[str, Any], path: str) -> bool:
    spec = prof.get("member") or {}
    if not spec:
        return True
    inside = any(path.startswith(p + "/") for p in spec.get("under") or [])
    return inside and not any(path.startswith(p + "/") for p in spec.get("except") or [])


def _place(prof, path: str, where, prefix: str | None) -> tuple[str | None, bool]:
    """What the profile says a place holds; a place it does not list, outside the
    member's blocks, is blocked from being read as the member's."""
    for p in (prof.get("places") or {}).get("list") or []:
        if (_reaches(p["at"], path) and set((p.get("where") or {}).items()) <= set(where)
                and p.get("prefix") in (None, prefix)):
            return p["is"], p["is"] is None
    return None, not _member(prof, path)


def _label(path: str, where, prefix: str | None) -> str:
    notes = [f"{k}={v}" for k, v in where] + ([prefix] if prefix else [])
    return path + (f" ({', '.join(notes)})" if notes else "")


def propose(sample: Sample, entries: list[Entry], algorithms: Iterable[str]) -> list[dict[str, Any]]:
    """Every value in the sample's document, and what the config says for it."""
    prof = sample.profile
    known = {a.upper() for a in algorithms}
    derived = engine.derived_targets(prof)
    kinds: dict[tuple, dict[str, Any]] = {}
    for node in _nodes(sample.root, sample.prefixes):
        value = _value(node)
        if (value is None or _structural(prof, node)
                or any(_reaches(d, node.path) for d in derived)):
            continue
        where = tuple(sorted(_qualifier(prof, node)
                             + tuple(_within(prof, node.element, sample.prefixes).items())))
        prefix = _prefix(value) if node.attr is not None else None
        slot = kinds.setdefault((node.path, where, prefix), {"count": 0, "example": value})
        slot["count"] += 1

    rows = []
    for (path, where, prefix), slot in kinds.items():
        entry = _entry_for(entries, path, dict(where), prefix)
        config_name = entry.name if entry else None
        place, blocked = _place(prof, path, where, prefix)
        algorithm, reason = engine.decide(config_name, place, blocked, known)
        rows.append({
            "path": path, "where": dict(where) or None, "prefix": prefix,
            "label": _label(path, where, prefix), "example": slot["example"].strip(),
            "count": slot["count"], "config_name": config_name, "algorithm": algorithm,
            "reason": reason, "suggested": place if place in known else None,
            "kept": reason.startswith(engine.KEPT),
        })
    return rows


# ---------------------------------------------------------------------------
# Writing: CDA's reader for the engine
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    index: int
    attr: str | None
    path: str
    prefix: str | None
    element: str
    nth: int              # this rule's nth place in the document: the first takes the record's value
    level: str | None     # the enclosing unit whose record fills it (the member), or the document


@dataclass
class Hole:
    """Where a document's repeated entries go: the element holding them, the
    place of the first, and the whitespace after an entry and after the last."""
    parent: int
    position: int
    between: str | None
    last: str | None


@dataclass
class Tpl:
    root: Any                     # lxml
    prefixes: dict[str, str]
    fills: list[Fill]
    codes: list[dict[str, Any]]
    base: str = ""                # an entry's path in its document; "" for the document
    hole: Hole | None = None      # a document whose entries a child unit writes
    table: Hole | None = None     # ...and where the narrative rows for them go
    row: Any = None               # an entry's narrative row in the sample (lxml)
    narrative: dict[str, Any] | None = None   # an entry's: how its row is written


def _fills(prof: dict[str, Any], root: Any, prefixes: dict[str, str], base: str,
           usable: list[Entry], inherited: dict[str, str] | None = None) -> list[Fill]:
    """The places of a template the config fills, and how."""
    fills: list[Fill] = []
    seen: dict[Entry, int] = {}
    for node in _nodes(root, prefixes, base):
        value = _value(node)
        if value is None:
            continue
        prefix = _prefix(value) if node.attr is not None else None
        attrs = {**node.element.attrib, **_within(prof, node.element, prefixes, inherited)}
        entry = _entry_for(usable, node.path, attrs, prefix)
        if entry is None:
            continue
        level = next((name for name, under in (prof.get("levels") or {}).items()
                      if name != "about" and any(node.path == u or node.path.startswith(u + "/")
                                                 for u in under)), None)
        fills.append(Fill(node.index, node.attr, node.path, prefix, entry.name.upper(),
                          seen.get(entry, 0), level))
        seen[entry] = seen.get(entry, 0) + 1
    return fills


@dataclass
class Item:
    root: Any                     # lxml
    tpl: Tpl
    holder: Any = None            # where the entries go, and their narrative rows
    body: Any = None
    # What each filled place held in the sample, and holds now: a narrative
    # that repeats the sample's value says the written one instead.
    said: dict[str, str] = field(default_factory=dict)


class Reader:
    """CDA for the engine: one document per top unit."""

    def __init__(self, sample: Sample):
        self.prolog = sample.prolog
        self.levels = sample.profile.get("levels")
        # Each document's places (its elements and attributes, with their
        # paths), walked once and kept until the document changes shape -- an
        # attribute added, a place dropped -- or is written out. The places
        # each path reaches are kept beside them. By id, with the object held,
        # so an id is never taken for another document's.
        self._walked: dict[int, tuple[Any, list[Node], dict[str, list[Node]]]] = {}

    # -- the sample's shape --------------------------------------------------

    def shape(self, sample: Sample, entries: list[Entry], known: set[str]) -> engine.Shape:
        prof = sample.profile
        usable = [e for e in entries if e.name.upper() in known]
        units = prof.get("units") or []
        if not units:
            raise SampleError(f"Profile {prof['name']} names no unit to repeat.")
        codes = list((prof.get("codes") or {}).get("list") or [])
        prefixes = sample.prefixes

        def document(spec: dict[str, Any]) -> engine.Unit:
            inner = (spec.get("units") or [None])[0]
            if inner is None:
                return engine.Unit(spec, [Tpl(sample.root, prefixes,
                                              _fills(prof, sample.root, prefixes, "", usable),
                                              codes)])
            if not inner.get("in"):
                raise SampleError(f"Profile {prof['name']}: a unit inside a document repeats "
                                  "'in' it (the entries of a section).")
            # The entries the inner unit repeats come out of the document; each is a
            # template of its own, and the document keeps where they were.
            doc = copy.deepcopy(sample.root)
            alive = list(doc.iter())          # keeps each element's id while paths is used
            paths = {id(n.element): n.path for n in _nodes(doc, prefixes) if n.attr is None}
            found = [el for el in alive if id(el) in paths and _reaches(inner["in"], paths[id(el)])
                     and el.getparent() is not None
                     and all(_first(el.getparent(), rel, prefixes) == str(want)
                             for rel, want in (inner.get("where") or {}).items())]
            if found:
                holder = found[0].getparent()
                found = [el for el in found if el.getparent() is holder]
            # An entry's row in the section's narrative (the one its text points
            # at) comes out with it: the rows are written again from the entries.
            narrative = inner.get("narrative")
            rows = [_row(doc, el, narrative) for el in found]
            kids = []
            for el, row in zip(found, rows):
                inherited = _within(prof, el, prefixes)
                own = copy.deepcopy(el)
                path = paths[id(el)]
                kids.append(engine.Unit(inner, [Tpl(own, prefixes,
                                                    _fills(prof, own, prefixes, path, usable,
                                                           inherited), codes, path,
                                                    row=copy.deepcopy(row) if row is not None
                                                    else None,
                                                    narrative=narrative)]))
            hole = table = None
            if found:
                holder = found[0].getparent()
                position = holder.index(found[0])
                between, last = found[0].tail, found[-1].tail
                for el in found:
                    holder.remove(el)
                hole = (holder, position, between, last)
            taken = [r for r in rows if r is not None]
            if taken and len(taken) == len(rows):
                body = taken[0].getparent()
                if all(r.getparent() is body for r in taken):
                    position = body.index(taken[0])
                    between, last = taken[0].tail, taken[-1].tail
                    for r in taken:
                        body.remove(r)
                    table = (body, position, between, last)
            order = list(doc.iter())
            if hole is not None:
                hole = Hole(order.index(hole[0]), *hole[1:])
            if table is not None:
                table = Hole(order.index(table[0]), *table[1:])
            for kid in kids:
                if table is None:
                    kid.items[0].row = None
            tpl = Tpl(doc, prefixes, _fills(prof, doc, prefixes, "", usable), codes, "", hole,
                      table)
            return engine.Unit(spec, [tpl], kids)

        def build(spec: dict[str, Any]) -> engine.Unit:
            # The unit that `starts` at ClinicalDocument is the document; one
            # around it (the member) only groups documents and has none of its own.
            if spec.get("starts"):
                return document(spec)
            inner = (spec.get("units") or [None])[0]
            if inner is None:
                raise SampleError(f"Profile {prof['name']}: unit {spec['name']} holds no "
                                  "document.")
            return engine.Unit(spec, [], [build(inner)])

        return engine.Shape([], [build(units[0])], units[0])

    # -- filling -------------------------------------------------------------

    def fill(self, tpl: Tpl, record: dict[str, Any], row: int, values) -> Item:
        root = copy.deepcopy(tpl.root)
        elements = list(root.iter())
        holder = elements[tpl.hole.parent] if tpl.hole is not None else None
        body = elements[tpl.table.parent] if tpl.table is not None else None
        fills = tpl.fills
        said: dict[str, str] = {}
        if values.vary:
            fills, elements = self._vary(tpl, root, elements, values, row)
        for f in fills:
            # A member's place takes the member's record and row, in each of the
            # member's documents alike.
            own, at_row = values.levels.get(f.level, (record, row)) if f.level else (record, row)
            # One record holds one value per element: the first place takes it, any
            # other (a second given name) the rule's.
            made = values(f.element, own, at_row) if f.nth == 0 else \
                values(f.element, {}, at_row * 100 + f.nth)
            if not made:
                continue
            element = elements[f.index]
            current = element.get(f.attr) if f.attr is not None else (element.text or "")
            base = current[len(f.prefix):].lstrip() if f.prefix else current
            rule = self._rule(tpl, f.path, f.element)
            coded, display = self._code(rule, made)
            new = coded if coded is not None else _fit(made, base.strip())
            if f.prefix:
                new = f.prefix + new
            if new == current:
                continue
            _put(element, f.attr, new)
            _record_said(said, base.strip(), new[len(f.prefix):].strip() if f.prefix
                         else new.strip())
            if f.attr == "code":
                # A displayName, the translations into other code systems and the
                # original text describe the sample's code, not the one written.
                if display:
                    element.set("displayName", display)
                else:
                    element.attrib.pop("displayName", None)
                for child in [c for c in element if _local(c) in ("translation", "originalText")]:
                    element.remove(child)
                if len(element) == 0 and not (element.text or "").strip():
                    element.text = None       # <code .../>, nothing left inside it
                for attr, value in _system(rule, new).items():
                    element.set(attr, value)
        return Item(root, tpl, holder, body, said)

    def _vary(self, tpl: Tpl, root: Any, elements: list[Any], values, row: int):
        """The document with its varied elements left out or repeated, and its
        fills moved to where their places now are; a copy's places are filled
        by the rules, as a second occurrence in the sample is."""
        alive = list(elements)            # keeps each element's id while it is used
        held: dict[int, list[Fill]] = {}
        for f in tpl.fills:
            held.setdefault(id(elements[f.index]), []).append(f)
        changed = False
        for vary in values.vary:
            if "@" in vary.at:
                continue
            groups: dict[int, list[Any]] = {}
            for n in _nodes(root, tpl.prefixes, tpl.base):
                if n.attr is None and n.element.getparent() is not None \
                        and _reaches(vary.at, n.path):
                    groups.setdefault(id(n.element.getparent()), []).append(n.element)
            # A member's place is the member's in each of the member's documents.
            path = vary.at.lstrip("/")
            level = next((name for name, under in (self.levels or {}).items()
                          if name != "about" and any(path == u or path.startswith(u + "/")
                                                     for u in under)), None)
            at_row = values.levels.get(level, (None, row))[1] if level else row
            for place, group in enumerate(groups.values()):
                had = len(group)
                n = values.times(vary, at_row, place, had)
                if n == had:
                    continue
                changed = True
                parent = group[0].getparent()
                for el in group[n:]:
                    if el.getnext() is None and el.getprevious() is not None:
                        el.getprevious().tail = el.tail
                    parent.remove(el)
                last = group[min(n, had) - 1] if n else None
                for i in range(had, n):
                    source = group[i % had]
                    copied = copy.deepcopy(source)
                    pairs = list(zip(source.iter(), copied.iter()))
                    alive.extend(c for _s, c in pairs)
                    for a, c in pairs:
                        held[id(c)] = [Fill(0, f.attr, f.path, f.prefix, f.element,
                                            f.nth + 1000 * i, f.level)
                                       for f in held.get(id(a), [])]
                    copied.tail = last.tail
                    if had > 1:
                        last.tail = group[0].tail
                    parent.insert(parent.index(last) + 1, copied)
                    last = copied
        if not changed:
            return tpl.fills, elements
        now = list(root.iter())
        fills = [Fill(i, f.attr, f.path, f.prefix, f.element, f.nth, f.level)
                 for i, el in enumerate(now) for f in held.get(id(el), [])]
        del alive
        return fills, now

    @staticmethod
    def _rule(tpl: Tpl, path: str, element: str) -> dict[str, Any] | None:
        return next((c for c in tpl.codes
                     if c["element"] == element and _reaches(c["at"], path)), None)

    @staticmethod
    def _code(rule: dict[str, Any] | None, value: str) -> tuple[str | None, str | None]:
        """The value as HL7 v3 codes it here, and its display, where the profile has codes:
        `map` from the store's codes, else the value as it is (a CPT code)."""
        if rule is None:
            return None, None
        if "map" not in rule:
            return value.strip(), None
        code = str(rule["map"].get(value.strip(), rule.get("default", value)))
        return code, (rule.get("display") or {}).get(code)

    # -- places, for derive rules ----------------------------------------------

    def _places(self, held: Any, root: Any, tpl: Tpl) -> tuple[list[Node], dict[str, list[Node]]]:
        kept = self._walked.get(id(held))
        if kept is None or kept[0] is not held:
            kept = (held, list(_nodes(root, tpl.prefixes, tpl.base)), {})
            self._walked[id(held)] = kept
        return kept[1], kept[2]

    def _reshaped(self, item: Item) -> None:
        """The document changed shape: its places are walked again when next asked."""
        self._walked.pop(id(item), None)

    def _found(self, item: Item, where: str) -> list[Node]:
        nodes, reached = self._places(item, item.root, item.tpl)
        if where not in reached:
            reached[where] = [n for n in nodes if _reaches(where, n.path)]
        return list(reached[where])

    def matches(self, item: Item, where: str) -> bool:
        return bool(self._found(item, where))

    def get(self, item: Item, where: str) -> Any:
        found = [n for n in self._found(item, where) if _value(n) is not None]
        return _value(found[0]) if found else None

    def set(self, item: Item, where: str, value: str) -> None:
        found = self._found(item, where)
        if not found and "/@" in where:
            # An attribute the element does not have yet (a nullFlavor) is added.
            element, attr = where.rsplit("/@", 1)
            found = [Node(n.index, n.element, attr, f"{n.path}/@{attr}")
                     for n in self._found(item, element) if n.attr is None]
            if found:
                self._reshaped(item)
        for n in found:
            _put(n.element, n.attr, value)

    def original(self, item: Item, where: str) -> Any:
        # The sample's own document, which is copied and never changed.
        nodes, reached = self._places(item.tpl, item.tpl.root, item.tpl)
        if where not in reached:
            reached[where] = [n for n in nodes if _reaches(where, n.path)]
        found = [n for n in reached[where] if _value(n) is not None]
        return _value(found[0]) if found else None

    def drop(self, item: Item, where: str) -> None:
        found = self._found(item, where)
        if found:
            self._reshaped(item)
        for n in reversed(found):
            if n.attr is not None:
                n.element.attrib.pop(n.attr, None)
            elif n.element.getparent() is not None:
                n.element.getparent().remove(n.element)

    def aliases(self, item: Item, old: str, new: str) -> dict[str, str]:
        return {}            # a document is referred to from outside it, not from another

    def relink(self, item: Item, mapping: dict[str, str]) -> None:
        return None

    # -- writing ---------------------------------------------------------------

    def lines(self, top: engine.Written) -> list[str]:
        """Each document, its entries put back where the sample had them."""
        # Written out and reshaped below: no place of this document is asked
        # for again, so what was walked is let go (the samples' own are kept).
        self._walked = {k: v for k, v in self._walked.items() if isinstance(v[0], Tpl)}
        out: list[str] = []

        def place(unit: engine.Written) -> None:
            if not unit.items:
                for child in unit.children:
                    place(child)
                return
            item = unit.items[0]
            hole = item.tpl.hole
            said = dict(item.said)
            if hole is not None:
                holder, body = item.holder, item.body
                kids = [c.items[0] for c in unit.children if c.items]
                for kid in kids:
                    said.update(kid.said)
                for k, kid in enumerate(kids):
                    el = kid.root
                    el.tail = hole.last if k == len(kids) - 1 else hole.between
                    holder.insert(hole.position + k, el)
                    if body is not None and kid.tpl.row is not None:
                        row = _written_row(kid, k + 1)
                        row.tail = item.tpl.table.last if k == len(kids) - 1 \
                            else item.tpl.table.between
                        body.insert(item.tpl.table.position + k, row)
            _reword_narrative(item.root, said)
            out.append(self.render(item))

        place(top)
        return out

    def render(self, item: Item) -> str:
        body = etree.tostring(item.root, encoding="unicode")
        return self.prolog + body + "\n"

    def envelope(self, sample: Sample) -> tuple[list[str], list[str]]:
        return [], []

    def closing(self, sample: Sample, count: int) -> list[str]:
        return []

    def after(self, sample: Sample) -> list[str]:
        return []

    def join(self, lines: list[str]) -> str:
        return "".join(lines)


# ---------------------------------------------------------------------------
# A section's narrative: a row per entry
# ---------------------------------------------------------------------------

def _at(element: Any, rel: str) -> list[tuple[Any, str | None]]:
    """The places a path relative to an element reaches: steps by local name,
    `*` any child element, a last `@x` an attribute."""
    steps = rel.strip("/").split("/")
    attr = steps.pop()[1:] if steps and steps[-1].startswith("@") else None
    found = [element]
    for step in steps:
        found = [c for e in found for c in e
                 if isinstance(c.tag, str) and (step == "*" or _local(c) == step)]
    return [(e, attr) for e in found]


def _shown(value: str) -> str:
    """An HL7 v3 timestamp as a narrative shows one (05 Mar 2026 09:22am)."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})?)?\d*(?:\.\d+)?(?:[+-]\d{4})?",
                     value)
    if not m:
        return value
    year, month, day, hour, minute = m.groups()
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    if not 1 <= int(month) <= 12:
        return value
    out = f"{day} {months[int(month) - 1]} {year}"
    if hour is not None:
        h = int(hour)
        out += f" {(h % 12) or 12:02d}:{minute or '00'}{'am' if h < 12 else 'pm'}"
    return out


def _said(element: Any, rel: str) -> str:
    """What a place says in a narrative: an attribute's or element's value; a
    time as a person reads it, an interval as low - high."""
    for el, attr in _at(element, rel):
        if attr is not None:
            value = el.get(attr)
            if value:
                return _shown(value) if _TS.fullmatch(value) else value
            continue
        if el.get("value"):
            return _shown(el.get("value"))
        ends = [_said(el, f"{end}/@value") for end in ("low", "high")]
        if any(ends):
            return " - ".join(e for e in ends if e)
        if el.text and el.text.strip():
            return el.text.strip()
    return ""


def _row(doc: Any, entry: Any, narrative: dict[str, Any] | None) -> Any:
    """The narrative row an entry's text points at (`#Procedure1`), if any."""
    if not narrative:
        return None
    target = next((el.get(attr) for el, attr in _at(entry, narrative["reference"])
                   if attr and el.get(attr)), None)
    if not target or not target.startswith("#"):
        return None
    return next((e for e in doc.iter() if isinstance(e.tag, str) and e.get("ID") == target[1:]),
                None)


def _written_row(kid: Item, n: int) -> Any:
    """The sample's row for an entry, written for the entry as it now is: the
    nth row's ids (`Procedure3`), the entry's references pointing at them, and
    the cells the profile names said from the entry (its code, its date)."""
    narrative = kid.tpl.narrative or {}
    row = copy.deepcopy(kid.tpl.row)
    renamed: dict[str, str] = {}
    for el in row.iter():
        if isinstance(el.tag, str) and el.get("ID"):
            old = el.get("ID")
            new = re.sub(r"\d+$", "", old) + str(n)
            renamed[old] = new
            el.set("ID", new)
    for el in kid.root.iter():
        if isinstance(el.tag, str) and _local(el) == "reference":
            value = el.get("value") or ""
            if value.startswith("#") and value[1:] in renamed:
                el.set("value", "#" + renamed[value[1:]])
    cells = [c for c in row if isinstance(c.tag, str) and _local(c) in ("td", "th")]
    statement = kid.root
    for cell, say in zip(cells, narrative.get("cells") or []):
        if not say:
            continue
        text = re.sub(r"\{([^}]+)\}", lambda m: _said(statement, m.group(1)), say).strip()
        for child in list(cell):
            cell.remove(child)
        cell.text = text
    return row


# A sample value shorter than this is not looked for in a narrative: a sex
# code or a state would match words that are not it.
_SAID_MIN = 3


def _record_said(said: dict[str, str], old: str, new: str) -> None:
    """That a place held `old` in the sample and holds `new` now, with a
    timestamp also as a narrative shows one (03 Feb 2014 09:22am)."""
    if len(old) < _SAID_MIN or old == new:
        return
    said.setdefault(old, new)
    if _TS.fullmatch(old) and _TS.fullmatch(new):
        said.setdefault(_shown(old), _shown(new))
        said.setdefault(_shown(old[:8]), _shown(new[:8]))


def _reword_narrative(root: Any, said: dict[str, str]) -> None:
    """A section's narrative (its `text`) that repeats a value the sample had
    in a filled place says the value written there instead.

    A narrative is free text no config can name a place in, so without this
    a patient's name or birth date in it would be the sample's in every
    document. Text that repeats no filled place stays as the sample has it."""
    if not said:
        return
    # Matched whatever the case: a narrative may say "JONES" where the
    # sample's place holds "Jones".
    by_case = {old.casefold(): new for old, new in said.items()}
    olds = sorted(said, key=len, reverse=True)
    found = re.compile("|".join(rf"(?<!\w){re.escape(o)}(?!\w)" for o in olds),
                       re.IGNORECASE)

    def reword(text: str | None) -> str | None:
        return found.sub(lambda m: by_case[m.group(0).casefold()], text) if text else text

    for block in root.iter(f"{{{NS}}}text"):
        parent = block.getparent()
        if parent is None or _local(parent) != "section":
            continue
        block.text = reword(block.text)
        for el in block.iterdescendants():
            el.text = reword(el.text) if isinstance(el.tag, str) else el.text
            el.tail = reword(el.tail)


def _system(rule: dict[str, Any] | None, code: str) -> dict[str, str]:
    """The code system a generated code is written with: the first of the
    rule's `systems` whose `match` the code fits (HCPCS Level II codes start
    with a letter, CPT codes with a digit)."""
    for option in (rule or {}).get("systems") or []:
        if re.fullmatch(option.get("match") or ".*", code):
            return dict(option.get("set") or {})
    return {}


def _fit(value: str, example: str) -> str:
    """A value written as the sample writes the place; a timestamp keeps the
    sample's precision, time of day and zone."""
    if _TS.fullmatch(example):
        day = engine._compact(value)
        if day:
            return day + example[8:]
    return engine.fit(value, example)


def stream(
    sample: Sample,
    entries: list[Entry],
    records: Iterable[dict[str, Any]],
    rules: dict[str, dict],
    *,
    seed: str = "cda",
    as_of: str = "2026-06-30",
    vary: Iterable[engine.Vary] = (),
) -> Iterator[str]:
    """The sample's document, once per record, one after another."""
    yield from engine.stream(Reader(sample), sample, entries, records, rules,
                             seed=seed, as_of=as_of, vary=vary)


def structure(sample: Sample) -> list[dict[str, Any]]:
    """The places a config can vary: each element of the sample, with the most
    times it occurs under one parent. What a unit repeats (a section's entries)
    is as many as the records have."""
    counted = []

    def units(specs) -> None:
        for spec in specs or []:
            if spec.get("in"):
                counted.append(spec["in"])
            units(spec.get("units"))

    units(sample.profile.get("units"))
    alive = list(sample.root.iter())      # keeps each element's id while it is used
    seen: dict[str, int] = {}
    under: dict[tuple[int, str], int] = {}
    for n in _nodes(sample.root, sample.prefixes):
        if n.attr is not None or any(_reaches(c, n.path) for c in counted):
            continue
        key = (id(n.element.getparent()), n.path)
        under[key] = under.get(key, 0) + 1
        seen[n.path] = max(seen.get(n.path, 0), under[key])
    del alive
    return [{"at": k, "count": v, "repeats": v > 1} for k, v in seen.items()]


def documents(text: str) -> list[str]:
    """Documents written one after another, apart: each starts with its declaration."""
    starts = [m.start() for m in re.finditer(r"(?m)^<\?xml ", text)]
    if not starts:
        return [text] if text.strip() else []
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)])]


class _Sink(io.RawIOBase):
    """Where a zip is written as it is made, emptied as each document goes out."""

    def __init__(self):
        self.held = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.held.extend(data)
        return len(data)

    def take(self) -> bytes:
        out = bytes(self.held)
        self.held.clear()
        return out


def package(chunks: Iterable[str], kind: str) -> Iterator[bytes]:
    """The documents as files in a zip, written as they come."""
    sink = _Sink()
    held = ""
    n = 0
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def put(doc: str):
            nonlocal n
            n += 1
            archive.writestr(f"{kind}-{n:05d}.xml", doc.encode("utf-8"))

        for chunk in chunks:
            held += chunk
            docs = documents(held)
            for doc in docs[:-1]:
                put(doc)
            held = docs[-1] if docs else ""
            yield sink.take()
        if held.strip():
            put(held)
    yield sink.take()


# ---------------------------------------------------------------------------
# Checking a written file
# ---------------------------------------------------------------------------

DISCLAIMER = ("Each document is checked as well-formed CDA with the header CDA R2 requires, "
              "for unique document ids and resolving narrative references, and for coded "
              "values where the profile lists their codes. Not a schema (CDA.xsd) or "
              "Schematron (C-CDA) validation.")


def _level(level: int, name: str, rules: int, findings: list[dict], notes=()) -> dict[str, Any]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    return {"level": level, "name": name, "rules": rules, "errors": errors,
            "warnings": warnings, "notes": list(notes), "findings": findings[:200],
            "status": "fail" if errors else ("warn" if warnings else "pass")}


def _finding(level: int, rule: str, message: str, where: str,
             severity: str = "error") -> dict[str, Any]:
    return {"level": level, "rule": rule, "message": message, "raw": where, "severity": severity}


def check(data: str | bytes, prof: dict[str, Any]) -> dict[str, Any]:
    """Written documents checked three ways: structure, identity, coded values."""
    if isinstance(data, bytes) and data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            docs = [(name, archive.read(name).decode("utf-8")) for name in archive.namelist()]
    else:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        docs = [(f"document {i + 1}", d) for i, d in enumerate(documents(text))]
    checks = prof.get("checks") or {}
    expected = prof.get("root", "ClinicalDocument")
    unit = "messages" if expected != "ClinicalDocument" else "documents"
    required = list(checks.get("required") or [])
    code_rules = list(checks.get("codes") or [])

    structure: list[dict] = []
    identity: list[dict] = []
    codes: list[dict] = []
    ids: dict[str, str] = {}
    for name, text in docs:
        try:
            _prolog, root, prefixes = _parse(text)
        except SampleError as exc:
            structure.append(_finding(1, "well-formed", str(exc), name))
            continue
        if root.tag != f"{{{NS}}}{expected}":
            structure.append(_finding(1, "root", f"The root is not {expected} in "
                                      "urn:hl7-org:v3.", name))
            continue
        nodes = list(_nodes(root, prefixes))
        for path in required:
            if not any(_reaches(path, n.path) for n in nodes):
                structure.append(_finding(1, "required", f"No {path}.", name))

        own = next((n for n in nodes if n.path == "id" and n.attr is None), None)
        key = f"{own.element.get('root')}^{own.element.get('extension')}" if own is not None else ""
        if key in ids:
            identity.append(_finding(2, "id-unique", f"Id {key} is also {ids[key]}'s.", name))
        elif key:
            ids[key] = name
        anchors = {e.get("ID") for e in root.iter() if isinstance(e.tag, str) and e.get("ID")}
        for n in nodes:
            if n.path.endswith("reference/@value"):
                target = n.element.get("value") or ""
                if target.startswith("#") and target[1:] not in anchors:
                    identity.append(_finding(2, "reference-resolves",
                                             f"{n.path} points at {target}, which no element "
                                             "of the document is.", name, "warning"))

        for rule in code_rules:
            allowed = set(rule["in"])
            for n in nodes:
                if _reaches(rule["at"], n.path):
                    value = _value(n)
                    if value is not None and value not in allowed:
                        codes.append(_finding(3, f"code:{rule['at']}",
                                              f"{rule['at']} is {value!r}; allowed: "
                                              f"{', '.join(sorted(allowed))}.", name))

    levels = [
        _level(1, f"Structure: well-formed, {expected}, required elements", 1 + len(required),
               structure),
        _level(2, f"{unit.capitalize()[:-1]} ids unique; narrative references resolve", 2,
               identity),
        _level(3, "Coded values in their value sets", len(code_rules), codes),
    ]
    passed = not any(lv["errors"] for lv in levels)
    summary = f"{len(docs):,} {unit}; {'no errors' if passed else 'errors found'}."
    return {"passed": passed, "summary": summary, "levels": levels,
            "disclaimer": checks.get("disclaimer") or DISCLAIMER}
