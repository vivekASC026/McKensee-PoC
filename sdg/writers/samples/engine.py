"""The one engine every sample format runs on.

It knows units, records, rules and derived positions -- never a segment, a
resource or an element of any particular format. The format's profile says
what repeats and what follows from what; the format's reader says how a file
is read and written and how a place is addressed. So the same engine writes
an 834, an 837 and (as further profiles and readers) FHIR, HL7 v2 and CDA.

Writing walks the profile's units over the records:

* a unit with `group` takes consecutive records sharing that element's value
  (one subscriber's claim lines), or, split further, those elements' values
  (`["CLAIM_ID", "SERVICE_DATE"]`: a claim's lines per day of service); a
  unit without takes one record. How many of a unit are written is always
  the records', never the profile's;
* each unit written is a copy of one of the sample's occurrences of it, taken
  in turn, filled from the first of its records (`row` numbers the seed a rule
  draws with: `count` counts the units of that level written so far, `nested`
  is the parent's row times 1000 plus the unit's place among its siblings);
* `derive` rules then set what the structure carries (`sequence`, `index`,
  `sum`), and the unit is rendered.

Derive rules: `sequence` (number an item across the file, re-pointing a
parent reference), `index` (an item's place among its siblings), `sum` (a
unit's total over its children), `scale` (a place following another, `by`,
in the sample's own ratio of the two: a paid amount stays the sample's share
of its charge), `total` (a header place, the file's total of `of`; the file
is held until the end to write it), `rename` (a new identity per written copy,
every reference to it in the unit following; `to` may use {id}, {n} and
{uuid}, a UUID made from the old identity and n), `set` (a fixed value, where
the place -- or the element it is on, `on` -- is), `drop` (remove a place).
A rule with `unit` applies to that unit's items only (a CDA entry's id, not
the document's other ids at the same path).

Varying (a config's `vary`): a place the sample has, in some documents and
not others, or once in one and three times in another. Each says where (`at`,
written as the format addresses an element), the share of the places it is
in that keep it (`present`, a percent) and how many times it then occurs
(`min` to `max`, else as often as the sample has it). Drawn from the seed and
the row, so the same request writes the same file; a copy past the sample's
own is filled by the rules as a second given name is. The reader applies it
to a template before filling it (`Values.times`).

A reader provides: shape(sample, entries, known) -> Shape;
fill(template, record, row, values) -> item; render(item) -> str;
lines(written unit) -> rendered pieces, children placed where the syntax puts
them; matches/get/set/original/drop(item, at), get being None where the item
has no such place; aliases(item, old, new) -> {how others refer to it: new}
and relink(item, mapping) for `rename`; envelope(sample) -> (before,
opening); closing(sample, count) -> lines; after(sample) -> lines;
join(lines) -> str.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Iterator

from sdg.writers.samples import profile as profiles
from sdg.writers.samples.base import SampleError

# Lines held before they are handed on as one piece of text.
CHUNK = 512


# ---------------------------------------------------------------------------
# The sample's shape, and what is written from it
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    """One occurrence of a unit in the sample: its own templates, then its children,
    then any templates of its own that follow them (`after`)."""
    spec: dict[str, Any]
    items: list[Any]
    children: list["Unit"] = field(default_factory=list)
    after: list[Any] = field(default_factory=list)


@dataclass
class Shape:
    head: list[Any]               # templates before the first unit
    units: list[Unit]             # the top unit's occurrences
    spec: dict[str, Any]          # the top unit


@dataclass
class Written:
    spec: dict[str, Any]
    items: list[Any]
    children: list["Written"]
    index: int                    # among its siblings, from 0
    modeled: bool                 # the sample's occurrence had children of its own
    after: list[Any] = field(default_factory=list)

    def walk(self) -> Iterator[Any]:
        """Every item, in the order it is written."""
        yield from self.items
        for child in self.children:
            yield from child.walk()
        yield from self.after


def cut(templates: list[Any], spec: dict[str, Any], starts, stops=None):
    """Templates before the first unit, and the unit's occurrences, each from a
    start to the next (or to a stop, whose templates and all after are dropped)."""
    head: list[Any] = []
    occurrences: list[list[Any]] = []
    for t in templates:
        if starts(t):
            occurrences.append([t])
        elif occurrences:
            occurrences[-1].append(t)
        else:
            head.append(t)
    if stops is not None:
        occurrences = [occ[:next((i for i, t in enumerate(occ) if stops(t)), len(occ))]
                       for occ in occurrences]
    return head, occurrences


# ---------------------------------------------------------------------------
# Varying: what is optional, and how often a place repeats
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Vary:
    at: str
    present: float = 100.0        # percent of the places it is in that keep it
    low: int | None = None        # occurrences where kept; None: as the sample has it
    high: int | None = None       # low = high: exactly that many
    # A mix: how many times, each in its share of the places that keep it --
    # ((1, 50), (2, 30), (3, 20)). Drawn instead of low..high.
    mix: tuple[tuple[int, float], ...] | None = None


def read_mix(value: Any, where: str = "mix") -> tuple[tuple[int, float], ...] | None:
    """A mix as a config or the screen writes it: [[1, 50], [2, 30]], {"1": 50},
    or "1:50, 2:30". Counts 0 to 50, shares that are not all nothing."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        pairs = [p.split(":", 1) for p in value.replace(";", ",").split(",") if p.strip()]
    elif isinstance(value, dict):
        pairs = list(value.items())
    else:
        pairs = list(value)
    out: list[tuple[int, float]] = []
    try:
        for pair in pairs:
            count, share = (pair[0], pair[1]) if len(pair) == 2 else (None, None)
            number = float(str(count).strip())
            if number != int(number):
                raise ValueError(count)
            out.append((int(number), float(str(share).strip().rstrip("%"))))
    except (TypeError, ValueError, IndexError):
        raise SampleError(f"{where}: a mix is how many times and its share, such as "
                          "1:50, 2:30, 3:20.") from None
    if not out or any(c < 0 or c > 50 or s < 0 for c, s in out) or sum(s for _, s in out) <= 0:
        raise SampleError(f"{where}: a mix is counts 0 to 50, each with a share, "
                          "the shares not all nothing.")
    return tuple(out)


def read_vary(body: dict | str | None) -> list[Vary]:
    """A config's `vary` list (any format's: it is the engine's, not the syntax's)."""
    from sdg.writers.samples.base import without_comments

    if isinstance(body, str):
        try:
            body = json.loads(without_comments(body))
        except ValueError:
            return []
    if not isinstance(body, dict):
        return []
    out = []
    for n, v in enumerate(body.get("vary") or []):
        if not isinstance(v, dict) or not str(v.get("at") or "").strip():
            raise SampleError(f"vary[{n}] names no place (at).")
        try:
            present = float(v.get("present", 100))
            low = None if v.get("min") in (None, "") else int(v["min"])
            high = None if v.get("max") in (None, "") else int(v["max"])
        except (TypeError, ValueError):
            raise SampleError(f"vary[{n}] ({v['at']}): present, min and max are numbers.") from None
        if not 0 <= present <= 100:
            raise SampleError(f"vary[{n}] ({v['at']}): present is a percent, 0 to 100.")
        if low is not None and high is None:
            high = low
        if high is not None and low is None:
            low = 1
        if low is not None and (low < 1 or high < low or high > 50):
            raise SampleError(f"vary[{n}] ({v['at']}): occurs min to max, 1 to 50, min first.")
        mix = read_mix(v.get("mix"), f"vary[{n}] ({v['at']})")
        out.append(Vary(str(v["at"]).strip(), present, None if mix else low,
                        None if mix else high, mix))
    return out


def write_vary(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Rows chosen on the screen as a config's `vary` list (checked by reading it back)."""
    out = []
    for row in rows or []:
        at = str(row.get("at") or "").strip()
        if not at:
            continue
        one: dict[str, Any] = {"at": at, "present": row.get("present", 100)}
        mix = read_mix(row.get("mix"), at)
        if mix:
            one["mix"] = [[c, s] for c, s in mix]
        else:
            for key in ("min", "max"):
                if row.get(key) not in (None, ""):
                    one[key] = row[key]
        out.append(one)
    read_vary({"vary": out})
    return out


def _draw(*parts: Any) -> float:
    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2 ** 64


def _times_v1(seed: str, vary: Vary, row: int, place: int, had: int) -> int:
    """Version 1 of how often a varied place occurs (`Values.times`)."""
    if _draw(seed, vary.at, row, place, "present") * 100 >= vary.present:
        return 0
    if vary.low is None:
        return had
    return vary.low + int(_draw(seed, vary.at, row, place, "times")
                          * (vary.high - vary.low + 1))


def pick_mix(draw: float, mix: tuple[tuple[int, float], ...]) -> int:
    """The count a draw in [0, 1) lands on, each count taking its share."""
    total = sum(s for _, s in mix)
    target, running = draw * total, 0.0
    for count, share in mix:
        running += share
        if target < running:
            return count
    return mix[-1][0]


def _times_v2(seed: str, vary: Vary, row: int, place: int, had: int) -> int:
    """Version 2: version 1, and a mix -- each count in its share."""
    if vary.mix is None:
        return _times_v1(seed, vary, row, place, had)
    if _draw(seed, vary.at, row, place, "present") * 100 >= vary.present:
        return 0
    return pick_mix(_draw(seed, vary.at, row, place, "times"), vary.mix)


def _register() -> None:
    from sdg.engine.generators import register_algorithm

    register_algorithm("occurrences", 1, _times_v1)
    register_algorithm("occurrences", 2, _times_v2)


_register()


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class Values:
    """An element's value for a record: the record's own, else its rule's.

    `levels` holds, for each unit being written and those enclosing it, the
    record and row it is filled from: a place the profile says is an enclosing
    unit's (the member's name in each of the member's documents) is filled
    from that unit's, so every copy under it agrees."""

    def __init__(self, rules: dict[str, dict], seed: str, as_of: str,
                 levels: dict[str, tuple[dict[str, Any], int]] | None = None,
                 vary: Iterable[Vary] = ()):
        self.rules = rules
        self.seed = seed
        self.as_of = as_of
        self.levels = levels or {}
        self.vary = tuple(vary)

    def within(self, name: str, record: dict[str, Any], row: int) -> "Values":
        return Values(self.rules, self.seed, self.as_of, {**self.levels, name: (record, row)},
                      self.vary)

    def times(self, vary: Vary, row: int, place: int, had: int) -> int:
        """How many times a varied place occurs in this copy: 0 where it is left
        out. `place` tells apart the places it is in (each of a Patient's
        names); `had` is how many the sample has there."""
        from sdg.engine.generators import algorithm

        return algorithm("occurrences")(self.seed, vary, row, place, had)

    def __call__(self, element: str, record: dict[str, Any], row_index: int) -> str:
        from sdg.writers import rule_values

        held = record.get(element)
        if held not in (None, ""):
            return rule_values._as_text(held)
        rule = self.rules.get(element)
        if rule is None:
            return ""
        return rule_values.produce({**rule, "element": element}, seed=self.seed,
                                   row_index=row_index, record=record, as_of=self.as_of)


# What the PHONE rule makes: a ten-digit number in one fixed shape, so a place
# can be given it the way the sample writes a phone.
_PHONE = re.compile(r"\(\d{3}\) \d{3}-\d{4}")


def _phone(digits: str, example: str) -> str:
    """A ten-digit phone in the sample's shape: its punctuation, country code
    and spacing kept, the digits poured in from the right; a place holding
    only part of one (HL7's area code, its local number) takes that part."""
    if not example.strip():
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if example.isdigit():
        return {3: digits[:3], 7: digits[3:]}.get(len(example), digits)
    if not re.fullmatch(r"[\d\s().+\-/]+", example) or not any(c.isdigit() for c in example):
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    slots = [i for i, c in enumerate(example) if c.isdigit()]
    poured = list(example)
    for i, d in zip(reversed(slots), reversed(digits)):
        poured[i] = d
    return "".join(poured)


def fit(value: str, example: str) -> str:
    """A value written the way the sample writes that place."""
    text = str(value)
    if _PHONE.fullmatch(text):
        return _phone(re.sub(r"\D", "", text), example)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", example):
        day = _compact(text)
        return f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else text
    if re.fullmatch(r"\d{8}-\d{8}", example):
        day = _compact(text)
        return f"{day}-{day}" if day else text
    if re.fullmatch(r"\d{8}", example):
        return _compact(text) or text
    if re.fullmatch(r"\d+\.\d{2}", example):
        try:
            return f"{float(text):.2f}"
        except ValueError:
            return text
    if example.isdigit() and re.fullmatch(r"[\d\- ]+", text):
        return re.sub(r"\D", "", text)
    if example.isupper() and any(ch.isalpha() for ch in example):
        return text.upper()
    return text


def _compact(text: str) -> str:
    if isinstance(text, (date, datetime)):
        return text.strftime("%Y%m%d")
    t = str(text)[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t.replace("-", "")
    return t if re.fullmatch(r"\d{8}", t) else ""


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------

def hint(name: str) -> str | None:
    """The element a masking algorithm's name says it makes."""
    return _hinted(name)[1]


def _hinted(name: str) -> tuple[bool, str | None]:
    """Whether a hint names it at all, and the element it says (None: one SDM
    makes no rule for, such as an e-mail address)."""
    flat = re.sub(r"[^A-Z0-9]", "", name.upper())
    for piece, element in profiles.name_hints():
        if piece in flat:
            return True, element
    return False, None


# A place left as the sample has it on purpose: no answer is wanted.
KEPT = "kept as in the sample"


def decide(config_name: str | None, place: str | None, blocked: bool,
           known: set[str]) -> tuple[str | None, str]:
    """The algorithm proposed for a place, and why."""
    if not config_name:
        return None, "copied from the sample"
    if config_name.upper() in known:
        return config_name.upper(), "the config names it"
    if blocked:
        return None, f"{KEPT}: not the member's own {config_name}"
    named, element = _hinted(config_name)
    if not place and named and element is None:
        return None, f"{KEPT}: SDM makes no {config_name}"
    guess = place or element
    if guess and guess in known:
        return guess, f"the config's {config_name}, read as {guess}"
    return None, f"the config's {config_name} is not an SDM algorithm; choose one"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _groups(records: Iterable[dict[str, Any]], spec: dict[str, Any]) -> Iterator[list[dict]]:
    key = spec.get("group")
    if not key:
        for record in records:
            yield [record]
        return
    yield from by(records, key)


def by(records: Iterable[dict[str, Any]], key: str | list[str]) -> Iterator[list[dict[str, Any]]]:
    """Consecutive records sharing a key's value, as one list each; a record
    without it is a group of its own. With several keys (["CLAIM_ID",
    "SERVICE_DATE"]) each run of the first is split by the others' values, in
    the order they first appear: a claim's lines, a group per day of service."""
    keys = [key] if isinstance(key, str) else list(key)
    current: list[dict[str, Any]] = []
    marker: Any = object()
    for index, record in enumerate(records):
        value = record.get(keys[0])
        value = value if value not in (None, "") else ("#", index)
        if current and value != marker:
            yield from _split(current, keys[1:])
            current = []
        marker = value
        current.append(record)
    if current:
        yield from _split(current, keys[1:])


def _split(run: list[dict[str, Any]], keys: list[str]) -> Iterator[list[dict[str, Any]]]:
    if not keys:
        yield run
        return
    parts: dict[tuple, list[dict[str, Any]]] = {}
    for record in run:
        held = tuple(record.get(k) if record.get(k) not in (None, "") else None for k in keys)
        parts.setdefault(held, []).append(record)
    yield from parts.values()


class _Writer:
    def __init__(self, reader, values: Values):
        self.reader = reader
        self.values = values
        self.counters: dict[str, int] = defaultdict(int)

    def unit(self, spec: dict[str, Any], occurrences: list[Unit], records: list[dict],
             index: int, parent_row: int, outer: Values | None = None) -> Written:
        occ = occurrences[index % len(occurrences)] if occurrences else None
        if spec.get("row") == "nested":
            row = parent_row * 1000 + index
        else:
            self.counters[spec["name"]] += 1
            row = self.counters[spec["name"]]
        record = records[0]
        values = (outer or self.values).within(spec["name"], record, row)
        items = [self.reader.fill(t, record, row, values) for t in (occ.items if occ else [])]
        children = []
        child = (spec.get("units") or [None])[0]
        if child is not None:
            for i, group in enumerate(_groups(records, child)):
                children.append(self.unit(child, occ.children if occ else [], group, i, row,
                                          values))
        after = [self.reader.fill(t, record, row, values) for t in (occ.after if occ else [])]
        return Written(spec, items, children, index, bool(occ and occ.children), after)


class _Derive:
    """The profile's derive rules, applied to each top unit as it is written."""

    def __init__(self, reader, rules: list[dict[str, Any]], head: list[Any]):
        self.reader = reader
        self.rules = rules
        self.counters: dict[int, int] = {}
        self.totals: dict[int, float] = {}
        for n, rule in enumerate(rules):
            if rule["op"] == "sequence":
                taken = [int(v) for v in (reader.get(i, rule["at"]) or "" for i in head
                                          if reader.matches(i, rule["at"])) if v.isdigit()]
                self.counters[n] = max(taken or [0])

    def apply(self, top: Written) -> None:
        r = self.reader
        for n, rule in enumerate(self.rules):
            op, at = rule["op"], rule["at"]
            if op == "sequence":
                mapping: dict[str, str] = {}
                for item in top.walk():
                    if not r.matches(item, at):
                        continue
                    self.counters[n] += 1
                    mapping[r.original(item, at)] = str(self.counters[n])
                    r.set(item, at, str(self.counters[n]))
                    parent = rule.get("parent")
                    if parent and r.get(item, parent) in mapping:
                        r.set(item, parent, mapping[r.get(item, parent)])
            elif op == "index":
                for unit in _units(top):
                    for item in unit.items:
                        if r.matches(item, at):
                            r.set(item, at, str(unit.index + 1))
            elif op == "sum":
                for unit in _units(top):
                    if not (unit.modeled and unit.items and r.matches(unit.items[0], at)
                            and r.get(unit.items[0], at) is not None):
                        continue
                    total = 0.0
                    for child in unit.children:
                        for item in child.walk():
                            for source in rule["of"]:
                                if r.matches(item, source):
                                    try:
                                        total += float(r.get(item, source))
                                    except (TypeError, ValueError):
                                        pass
                    r.set(unit.items[0], at, f"{total:.{int(rule.get('decimals', 2))}f}")
            elif op == "scale":
                # A value that follows another by the sample's own ratio: a paid
                # amount stays the sample's share of the charge it pays.
                for item in top.walk():
                    if not (r.matches(item, at) and r.matches(item, rule["by"])):
                        continue
                    try:
                        was, base = float(r.original(item, at)), float(r.original(item, rule["by"]))
                        now = float(r.get(item, rule["by"]))
                    except (TypeError, ValueError):
                        continue
                    if base:
                        r.set(item, at, _amount(was / base * now, rule, r.original(item, at)))
            elif op == "total":
                # Added up over the whole file; set on the header once it is written.
                for item in top.walk():
                    for source in rule["of"]:
                        if r.matches(item, source):
                            try:
                                self.totals[n] = self.totals.get(n, 0.0) + float(r.get(item, source))
                            except (TypeError, ValueError):
                                pass
            elif op == "rename":
                scope = list(_scoped(top, rule))
                # Each written copy of a sample record is a record of its own: its
                # identity changes, and every reference to it in the unit with it.
                # {n} counts every item renamed in the file, so two copies of one
                # sample record never share an identity.
                mapping: dict[str, str] = {}
                for item in scope:
                    if not r.matches(item, at):
                        continue
                    self.counters[n] = self.counters.get(n, 0) + 1
                    old = r.get(item, at)
                    made = uuid.uuid5(uuid.NAMESPACE_URL, f"{old}#{self.counters[n]}")
                    new = str(rule.get("to") or "{id}-{n}").format(
                        id=old, n=self.counters[n], uuid=made)
                    r.set(item, at, new)
                    mapping.update(r.aliases(item, old, new))
                for item in top.walk():
                    r.relink(item, mapping)
            elif op == "set":
                # A fixed value, where the place (or the element it is on, `on`) is.
                for item in _scoped(top, rule):
                    if r.matches(item, rule.get("on") or at):
                        r.set(item, at, str(rule["value"]))
            elif op == "drop":
                for item in _scoped(top, rule):
                    if r.matches(item, at):
                        r.drop(item, at)
            else:
                raise SampleError(f"Profile rule {op!r} is not one the engine knows.")


    @property
    def holds_head(self) -> bool:
        """A header place is a total of the file: the header waits for the end."""
        return any(rule["op"] == "total" for rule in self.rules)

    def finish(self, head: list[Any]) -> None:
        """The file's totals, set on the header items they are at."""
        for n, rule in enumerate(self.rules):
            if rule["op"] != "total":
                continue
            for item in head:
                if self.reader.matches(item, rule["at"]):
                    self.reader.set(item, rule["at"], _amount(
                        self.totals.get(n, 0.0), rule, self.reader.original(item, rule["at"])))


def _amount(value: float, rule: dict[str, Any], example: str | None) -> str:
    """An amount to the rule's places (2); with none in the sample's, as few as it needs."""
    text = f"{value:.{int(rule.get('decimals', 2))}f}"
    if example is not None and "." not in str(example) and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _scoped(top: Written, rule: dict[str, Any]) -> Iterator[Any]:
    """The items a rule applies to: all of them, or one unit's (`unit`)."""
    if not rule.get("unit"):
        yield from top.walk()
        return
    for unit in _units(top):
        if unit.spec.get("name") == rule["unit"]:
            yield from unit.items
            yield from unit.after


def _units(top: Written) -> Iterator[Written]:
    yield top
    for child in top.children:
        yield from _units(child)


def derived_targets(profile: dict[str, Any]) -> list[str]:
    """Places a derive rule sets, which nobody chooses an algorithm for."""
    return [rule["at"] for rule in profile.get("derive") or []]


def stream(reader, sample, entries, records: Iterable[dict[str, Any]], rules: dict[str, dict],
           *, seed: str, as_of: str, vary: Iterable[Vary] = ()) -> Iterator[str]:
    """The file modeled on the sample, written for these records. Lookups
    keep what they worked out for the whole file (each row's values drawn
    from the same lists), not only while a plan runs."""
    from sdg.engine.generators.lookup import caching_each

    return caching_each(_stream(reader, sample, entries, records, rules, seed=seed,
                                as_of=as_of, vary=vary))


def _stream(reader, sample, entries, records: Iterable[dict[str, Any]], rules: dict[str, dict],
            *, seed: str, as_of: str, vary: Iterable[Vary] = ()) -> Iterator[str]:
    shape = reader.shape(sample, entries, set(rules))
    values = Values(rules, seed, str(as_of), vary=vary)
    writer = _Writer(reader, values)
    head = [reader.fill(t, {}, 0, values) for t in shape.head]
    derive = _Derive(reader, sample.profile.get("derive") or [], head)
    # A total in the header (an 835's BPR02, its payments added up) is known at
    # the end only: the file is then held whole and the header rendered last.
    held = derive.holds_head

    before, opening = reader.envelope(sample)
    out: list[str] = list(before)
    body: list[str] = [*opening, *([] if held else (reader.render(i) for i in head))]
    counted = 0              # transaction lines already handed on

    def flush() -> str:
        nonlocal counted
        counted += len(body)
        text = reader.join([*out, *body])
        out.clear()
        body.clear()
        return text

    written = 0
    for group in _groups(records, shape.spec):
        top = writer.unit(shape.spec, shape.units, group, written, 0)
        derive.apply(top)
        body.extend(reader.lines(top))
        written += 1
        if len(body) >= CHUNK and not held:
            yield flush()

    if written == 0:
        raise SampleError("No records to write; generate some first.")
    if held:
        derive.finish(head)
        body[len(opening):len(opening)] = [reader.render(i) for i in head]
    body.extend(reader.closing(sample, counted + len(body)))
    yield flush()
    yield reader.join(reader.after(sample))
