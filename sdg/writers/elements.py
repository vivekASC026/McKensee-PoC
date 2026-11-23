"""Records keyed by element, which is what every file this tool writes reads.

A file says DOB, never MEME_BIRTH_DT. A column is a fact about one database; an
element is what the value means, and it is the only name a format description,
a layout or a rule uses. So everything that feeds a writer arrives here keyed
by element, and the translation from columns happens once -- through the
plan's own bindings, the same ones generation already runs on.

Three ways a record comes to exist:

    from a run        the rows the plan generated, so a file describes the
                      same population a database load would write
    from a pattern    new members whose elements take the pattern's values --
                      what an 834 for a pattern is
    for a pattern     claims for the members the pattern actually holds --
                      what an 837 for a pattern is, because a claim is always
                      made by somebody who exists

A pattern is a saved list of elements and their values. Applied to rules, it
replaces the rule for each element it names and leaves the rest alone.
"""

from __future__ import annotations

import zlib
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Iterator

from sdg.writers import rule_values
from sdg import packs
from sdg.packs import held, held_or


# ---------------------------------------------------------------------------
# Bindings: which column carries which element, per table
# ---------------------------------------------------------------------------

def bindings(plan) -> dict[str, dict[str, str]]:
    """Table -> {column: element}, for every column the plan bound to one."""
    out: dict[str, dict[str, str]] = {}
    for table in plan.tables:
        bound = {
            field.name: str(field.element).strip().upper()
            for field in table.fields
            if field.element
        }
        if bound:
            out[table.name] = bound
    return out


def _table_with(found: dict[str, dict[str, str]], *elements: str,
                without: Iterable[str] = ()) -> str | None:
    """The table that carries every one of these elements, and none of those."""
    wanted, refused = set(elements), set(without)
    for table, bound in found.items():
        carried = set(bound.values())
        if wanted <= carried and not (refused & carried):
            return table
    return None


def _column_for(found: dict[str, dict[str, str]], table: str | None,
                element: str) -> str | None:
    for column, bound in (found.get(table or "") or {}).items():
        if bound == element:
            return column
    return None


def as_elements(row: dict[str, Any] | None, bound: dict[str, str],
                into: dict[str, Any] | None = None) -> dict[str, Any]:
    """A row's values under the elements its columns carry.

    Written over `into`, so the more specific row is applied last and wins: a
    member's own first name over the subscriber's.
    """
    out = into if into is not None else {}
    for column, element in bound.items():
        if row and column in row and row[column] not in (None, ""):
            out[element] = row[column]
    return out


# Each record's table, found by the elements it carries (and not others).
RECORD_TABLES = held("pack:plan.record_tables")
# The element each role's records are joined to the records below them by
# (pack.json plan.joins: role -> element).
JOINS: dict[str, str] = held("pack:plan.joins")


def tables(plan) -> dict[str, str | None]:
    """Each record's table, found by what it carries."""
    found = bindings(plan)
    return {record: _table_with(found, *spec["carries"], without=tuple(spec.get("without", ())))
            for record, spec in RECORD_TABLES.items()}


# ---------------------------------------------------------------------------
# From a run
# ---------------------------------------------------------------------------

def run_members(result, plan, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """One record per generated primary record, its parent's elements beside
    it where the pack has a parent of the primary."""
    found = bindings(plan)
    where = tables(plan)
    pack = packs.active()
    primaries = where.get(pack.primary)
    parents = where.get(pack.with_role("parent_of_primary") or "")
    if not primaries:
        return
    primary_parent = _column_for(found, primaries, JOINS.get("parent_of_primary", ""))
    parent_key = _column_for(found, parents, JOINS.get("parent_of_primary", ""))
    by_parent = {
        row.get(parent_key): row for row in result.rows(parents or "")
    } if parents and parent_key else {}

    for position, primary in enumerate(result.rows(primaries)):
        if limit is not None and position >= limit:
            return
        record: dict[str, Any] = {}
        if primary_parent:
            as_elements(by_parent.get(primary.get(primary_parent)),
                        found.get(parents or "", {}), record)
        yield as_elements(primary, found[primaries], record)


def run_claim_lines(result, plan, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """One record per generated line of a child record, its child record and
    primary record beside it."""
    found = bindings(plan)
    where = tables(plan)
    pack = packs.active()
    lines = where.get("lines")
    children, primaries = where.get(pack.child or ""), where.get(pack.primary)
    if not lines:
        return
    line_child = _column_for(found, lines, JOINS.get("child", ""))
    child_key = _column_for(found, children, JOINS.get("child", ""))
    child_primary = _column_for(found, children, JOINS.get("primary", ""))
    primary_key = _column_for(found, primaries, JOINS.get("primary", ""))

    by_child = {row.get(child_key): row for row in result.rows(children or "")} \
        if children and child_key else {}
    by_primary = {row.get(primary_key): row for row in result.rows(primaries or "")} \
        if primaries and primary_key else {}

    for position, line in enumerate(result.rows(lines)):
        if limit is not None and position >= limit:
            return
        child = by_child.get(line.get(line_child), {})
        primary = by_primary.get(child.get(child_primary)) if child_primary else None
        record: dict[str, Any] = {}
        as_elements(primary, found.get(primaries or "", {}), record)
        as_elements(child, found.get(children or "", {}), record)
        yield as_elements(line, found[lines], record)


# ---------------------------------------------------------------------------
# A pattern, as rules
# ---------------------------------------------------------------------------

def pattern_rules(profile: dict[str, Any], catalog, *, kind: str = "member",
                  as_of: str = "2026-06-30") -> dict[str, dict[str, Any]]:
    """The rule a pattern implies for each element it names.

    A band stays a band and a distribution stays a distribution: ages 51 to
    75 become a birth-date window, 54/46 becomes a weighted list. Nothing the
    pattern does not name is touched.
    """
    from sdg.analyzer.bridge import to_pattern
    from sdg.plan.patterns import element_keys, match_derivation, match_element

    found = to_pattern(profile, kind=kind, as_of=as_of)
    index = element_keys(catalog)
    reference = date.fromisoformat(str(as_of)[:10])
    out: dict[str, dict[str, Any]] = {}

    for parameter in found.parameters:
        derived = match_derivation(parameter.label)
        if derived is not None:
            element = derived[1]["element"]
            if parameter.min is None or parameter.max is None:
                continue
            months = str(parameter.unit or "").lower().startswith("month")
            latest = _back(reference, int(parameter.min), months)
            earliest = _back(reference, int(parameter.max) + 1, months) + timedelta(days=1)
            out[element] = {
                "element": element, "gen_method": "date",
                "format_pattern": latest.isoformat(),
                "start_value": max(1, (latest - earliest).days + 1),
            }
            if parameter.bands:
                # Ages (or months) spread as the pattern's: each slice a window of
                # days back from the latest day, drawn at its share.
                per = 30.4375 if months else 365.25
                start = float(parameter.min)
                out[element]["bands"] = [
                    [-int(round((high - start) * per)), -int(round((low - start) * per)),
                     share]
                    for low, high, share in parameter.bands]
            continue

        element, _near = match_element(parameter.label, index)
        if element is None:
            continue
        if not parameter.record_field and (out.get(element) or {}).get("record_key"):
            # The same field counted on its own (a plan's share) never replaces
            # it drawn with the others of its record (the plan with its own
            # product and group), or plans and products pair at random.
            continue
        if parameter.record_field and parameter.values:
            out[element] = {
                "element": element, "gen_method": "lookup",
                "values": list(parameter.values), "weights": list(parameter.weights or []),
                "record_key": parameter.record_key(), "field": parameter.record_field,
            }
            if parameter.record_group and parameter.record_group != "address":
                # A line's procedure is drawn from the combinations holding
                # the diagnosis its claim already has, and the other way round.
                fields = sorted({k for r in parameter.values if isinstance(r, dict)
                                 for k in r})
                out[element]["given"] = {f: f for f in fields
                                         if f != parameter.record_field}
        elif parameter.values:
            out[element] = {
                "element": element, "gen_method": "lookup",
                "values": [str(v) for v in parameter.values],
                "weights": list(parameter.weights or []),
            }
        elif parameter.value is not None:
            out[element] = {"element": element, "gen_method": "constant",
                            "domain": str(parameter.value)}
        elif parameter.min is not None and parameter.max is not None:
            out[element] = {"element": element, "gen_method": "range",
                            "min": parameter.min, "max": parameter.max}
            if parameter.bands:
                # Amounts spread as the pattern's: each slice drawn at its share.
                out[element]["bands"] = [list(b) for b in parameter.bands]
    return out


def _back(day: date, amount: int, months: bool) -> date:
    if months:
        month = day.month - 1 - amount
        year = day.year + month // 12
        return _safe(day, year, month % 12 + 1)
    return _safe(day, day.year - amount, day.month)


def _safe(day: date, year: int, month: int) -> date:
    try:
        return day.replace(year=year, month=month)
    except ValueError:
        return day.replace(year=year, month=month, day=28)


def with_pattern(rules: dict[str, dict], pattern: dict[str, dict]) -> dict[str, dict]:
    """The rules, with the pattern's replacing the ones it names."""
    merged = dict(rules)
    for element, rule in pattern.items():
        base = merged.get(element) or {}
        # The base rule's decoration -- a prefix, a width -- survives; what the
        # value is drawn from is the pattern's.
        merged[element] = {
            **{k: base[k] for k in ("prefix", "postfix", "data_type") if k in base},
            **rule,
        }
    return merged


# ---------------------------------------------------------------------------
# New members, for an 834
# ---------------------------------------------------------------------------

def new_members(count: int, rules: dict[str, dict] | None = None, *,
                seed: str = "members", as_of: str = "2026-06-30") -> list[dict[str, Any]]:
    """`count` members nobody has made yet, in families.

    What a member holds here is what makes them one of a family; every other
    element is drawn from its rule when the file is written, at the member's
    own position, so the same seed writes the same members. The pattern
    reaches them through the rules it replaced.
    """
    from sdg.engine.generators import algorithm

    return algorithm("members")(max(0, int(count)), rules or {}, seed, as_of)


def _members_v1(count: int, rules: dict[str, dict], seed: str, as_of: str):
    """Version 1: each member on their own, every element drawn from its rule,
    their relationship too -- a dependent whose subscriber is nobody."""
    return [{} for _ in range(count)]


# What a family shares, written on every member of it as the subscriber's.
# Fixed with version 2 of `members`: sharing another element is version 3.
_FAMILY = tuple(held_or("pack:files.household.family", ()))
# A spouse or life partner, as RELATIONSHIP_CODE says it (FACETS and X12).
_PARTNER = set(held_or("pack:files.household.partner_codes", set()))
_SPOUSE = {"S", "01"}
# A child is at least this many years younger than the subscriber, and
# single while under this age.
_CHILD_YEARS = held_or("pack:files.household.child_years", 15)
_ADULT_YEARS = 18
# How many subscribers back, the latest first, a dependent looks for theirs.
_WINDOW = 30


def _members_v2(count: int, rules: dict[str, dict], seed: str, as_of: str):
    """Version 2: families. Each member is drawn as version 1 draws them --
    their relationship (with their marital status, where the two are drawn
    together) and birth date at their own place -- so a file keeps the
    pattern's mix and ages exactly. Then each dependent joins a subscriber
    before them in the file: a partner one who has none, a child one born at
    least 15 years earlier. Where none fits -- a pattern of young people whose
    parents are older than it holds -- their subscriber is made but not
    written, as an 834 adding a dependent names a subscriber enrolled before;
    a later brother or sister can join them. A dependent shares the
    subscriber's ID, last name, address, group and plan, and names them.
    Without a rule drawing subscribers, version 1."""
    rule = rules.get("RELATIONSHIP_CODE")
    codes = _weighted_codes(rule, "RELATIONSHIP_CODE")
    if not any(c in SUBSCRIBER for c in codes):
        return _members_v1(count, rules, seed, as_of)
    # Where marital status is drawn on its own in FACETS' letters, a child under
    # 18 is single (S) and a subscriber and spouse are married (M). Drawn with
    # the relationship it already agrees with it, and keeps the pattern's shares.
    marital = rules.get("MARITAL_STATUS")
    married = (not _drawn_with(marital, "MARITAL_STATUS", "RELATIONSHIP_CODE")
               and {"S", "M"} <= _codes(marital, "MARITAL_STATUS"))

    def drawn(element: str, at_seed: str, place: int, record: dict[str, Any]) -> str:
        return rule_values.produce({**rules[element], "element": element}, seed=at_seed,
                                   row_index=place, as_of=as_of, context=record)

    def subscriber(record: dict[str, Any], at_seed: str, place: int) -> dict[str, Any]:
        for element in (*_FAMILY, "FIRST_NAME", "MEMBER_ID"):
            if element in rules and record.get(element) in (None, ""):
                record[element] = drawn(element, at_seed, place, record)
        return record

    out: list[dict[str, Any]] = []
    heads: list[dict[str, Any]] = []   # the latest last, outside ones too
    for place in range(1, count + 1):
        record: dict[str, Any] = {}
        code = str(drawn("RELATIONSHIP_CODE", seed, place, record)).strip().upper()
        record["RELATIONSHIP_CODE"] = code
        if "DOB" in rules:
            record["DOB"] = drawn("DOB", seed, place, record)
        if code in SUBSCRIBER or not code:
            heads.append(subscriber(record, seed, place))
            out.append(record)
            continue
        partner = code in _PARTNER
        head = next((h for h in reversed(heads[-_WINDOW:])
                     if (not h.get("_partner") if partner
                         else _gap(h.get("DOB"), record.get("DOB")) >= _CHILD_YEARS)), None)
        if head is None:
            # Their subscriber, made at this place from a seed of its own and
            # older than a child by a generation; not a member of this file.
            head = subscriber({"_outside": True}, f"{seed}:subscriber", place)
            if record.get("DOB") and not partner:
                older = _years_before(record["DOB"], 25 + int(
                    _draw(seed, "parent age", place) * 16))
                if older:
                    head["DOB"] = older
            heads.append(head)
        if partner:
            head["_partner"] = True
        for element in _FAMILY:
            if head.get(element) not in (None, ""):
                record[element] = head[element]
        for element in ("FIRST_NAME", "LAST_NAME", "MEMBER_ID"):
            if head.get(element) not in (None, ""):
                record[f"SUBSCRIBER_{element}"] = head[element]
        if married and code in _SPOUSE:
            record["MARITAL_STATUS"] = "M"
            if not head.get("_outside"):
                head["MARITAL_STATUS"] = "M"
        elif married and not partner and _years(record.get("DOB"), as_of) < _ADULT_YEARS:
            record["MARITAL_STATUS"] = "S"
        out.append(record)
    for head in heads:
        head.pop("_partner", None)
        head.pop("_outside", None)
    return out


def _gap(older: Any, younger: Any) -> float:
    """Years from one birth date to the next; -1 where either is not a date."""
    try:
        first, second = date.fromisoformat(str(older)[:10]), date.fromisoformat(str(younger)[:10])
    except ValueError:
        return -1
    return (second - first).days / 365.25


def _years_before(born: str, years: int) -> str:
    """The birth date `years` earlier; "" where `born` is not a date, so a
    DOB rule that writes another format leaves the primary record's own date."""
    try:
        day = date.fromisoformat(str(born)[:10])
    except ValueError:
        return ""
    return _safe(day, day.year - years, day.month).isoformat()


def _weighted_codes(rule: dict[str, Any] | None, element: str) -> dict[str, float]:
    """Each value a lookup rule draws, with its weight; one drawn with others
    (a combination) by the value of this element. Empty for any other rule."""
    from collections import Counter

    from sdg.engine.rules import RuleError, generator_for

    if not rule:
        return {}
    try:
        found = generator_for({**rule, "element": element}, element)
    except RuleError:
        return {}
    if not found or found[0] != "lookup":
        return {}
    params = found[1]
    values = params.get("values") or []
    weights = params.get("weights") or [1.0] * len(values)
    field = params.get("field") or element
    totals: Counter = Counter()
    for value, weight in zip(values, weights):
        code = value.get(field) if isinstance(value, dict) else value
        if code not in (None, ""):
            totals[str(code).strip().upper()] += float(weight)
    return dict(totals)


def _drawn_with(rule: dict[str, Any] | None, element: str, other: str) -> bool:
    """Whether a rule draws its element together with `other`."""
    from sdg.engine.rules import RuleError, generator_for

    if not rule:
        return False
    try:
        found = generator_for({**rule, "element": element}, element)
    except RuleError:
        return False
    params = (found or ("", {}))[1]
    values = params.get("values") or []
    return other in (params.get("given") or {}) or any(
        isinstance(v, dict) and other in v for v in values)


def _codes(rule: dict[str, Any] | None, element: str) -> set[str]:
    return set(_weighted_codes(rule, element))


def _years(born: Any, as_of: str) -> int:
    """Age in whole years on the as-of date; 0 where the date is not one."""
    try:
        birth, today = date.fromisoformat(str(born)[:10]), date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return 0
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


# ---------------------------------------------------------------------------
# Existing members, and claims for them, for an 837
# ---------------------------------------------------------------------------

def claims_for(
    members: list[dict[str, Any]],
    rules: dict[str, dict],
    *,
    claim_level: list[str],
    line_level: list[str],
    claims_per_member: int = 1,
    lines_per_claim: int = 1,
    seed: str = "claims",
    as_of: str = "2026-06-30",
    limit: int | None = None,
    claims_of: Callable[[int], int] | None = None,
    lines_of: Callable[[int], int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Claim lines for members who exist.

    Everything a claim says -- its status, diagnosis, dates, charges -- is
    drawn from the rules, which a pattern has already replaced where it names
    an element. The member is read, never made.

    `claims_of`, given a member's place (from one), says how many claims that
    member has, none for a member without; else every member has
    `claims_per_member`. `lines_of`, given a claim's number, says how many
    lines it has; else every claim has `lines_per_claim`.
    """
    # A claim's totals: elements whose rule sums an element of its lines.
    totals = {e: rule_values.summed(rules.get(e)) for e in [*claim_level, *line_level]}
    totals = {e: of for e, of in totals.items() if of}
    # What a child record and each of its lines are known by, the pack's
    # (health: CLAIM_ID and LINE_NUMBER; a bank: ACCOUNT_ID and TXN_SEQ).
    record_id, line_id = packs.active().child_anchors()
    record_id, line_id = record_id or "CLAIM_ID", line_id or "LINE_NUMBER"
    claim_no = 0
    line_no = 0
    written = 0
    # Decided once for the store, never per record: one record without a name
    # in a store that holds names is written as it always was.
    nameless = holds_no_names(members)
    for place, member in enumerate(members, start=1):
        count = claims_of(place) if claims_of is not None else max(1, claims_per_member)
        for _ in range(count):
            claim_no += 1
            claim = _as_person(member, rules, seed, place, as_of, nameless=nameless)
            claim[record_id] = (_made(record_id, rules, seed, claim_no, as_of)
                                or f"{seed[:4].upper()}{claim_no:08d}")
            for element in claim_level:
                if element in rules and element not in totals:
                    claim[element] = _made(element, rules, seed, claim_no, as_of, claim)
            lines = []
            many = lines_of(claim_no) if lines_of is not None else max(1, lines_per_claim)
            for number in range(1, max(1, many) + 1):
                line_no += 1
                line = dict(claim)
                line[line_id] = number
                for element in line_level:
                    if element in rules and element not in totals:
                        line[element] = _made(element, rules, seed, line_no, as_of, line)
                lines.append(line)
            for element, of in totals.items():
                total = _total(lines, of)
                for line in lines:
                    line[element] = total
            for line in lines:
                yield line
                written += 1
                if limit is not None and written >= limit:
                    return


Mix = tuple[tuple[int, float], ...]


def _draw(*parts: Any) -> float:
    import hashlib

    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2 ** 64


def _pick(draw: float, mix: Mix) -> int:
    """The count a draw lands on, each count taking its share."""
    total = sum(s for _, s in mix)
    target, running = draw * total, 0.0
    for count, share in mix:
        running += share
        if target < running:
            return count
    return mix[-1][0]


def _claims_of_v1(seed: str, place: int, share: float, low: int, high: int) -> int:
    """Version 1 of how many claims a member has: none for a member outside
    the share, else low to high, drawn evenly."""
    if _draw(seed, "claimed", place) * 100 >= share:
        return 0
    return low + int(_draw(seed, "claims", place) * (high - low + 1))


def _claims_of_v2(seed: str, place: int, share: float, low: int, high: int,
                  mix: Mix | None = None) -> int:
    """Version 2: version 1, and a mix -- each count of claims in its share."""
    if mix is None:
        return _claims_of_v1(seed, place, share, low, high)
    if _draw(seed, "claimed", place) * 100 >= share:
        return 0
    return _pick(_draw(seed, "claims", place), mix)


def _lines_of_v1(seed: str, claim: int, low: int, high: int, mix: Mix | None = None) -> int:
    """Version 1 of how many lines a claim has: exactly, low to high, or a mix."""
    if mix is not None:
        return max(1, _pick(_draw(seed, "lines", claim), mix))
    return low + int(_draw(seed, "lines", claim) * (high - low + 1))


def _bounds(low: int, high: int) -> tuple[int, int]:
    low, high = max(1, int(low)), max(1, int(high))
    return min(low, high), max(low, high)


def claims_of(seed: str, share: float, low: int, high: int,
              mix: Mix | None = None) -> Callable[[int], int]:
    """How many claims each member has, by their place: `share` percent of the
    members have claims, each `low` to `high` of them, or as `mix` shares them."""
    from sdg.engine.generators import algorithm

    low, high = _bounds(low, high)
    share = max(0.0, min(100.0, float(share)))
    if mix:
        return lambda place: algorithm("claims per member")(seed, place, share, low, high, mix)
    return lambda place: algorithm("claims per member")(seed, place, share, low, high)


def lines_of(seed: str, low: int, high: int, mix: Mix | None = None) -> Callable[[int], int]:
    """How many lines each claim has, by its number: `low` to `high`, or as `mix`."""
    from sdg.engine.generators import algorithm

    low, high = _bounds(low, high)
    return lambda claim: algorithm("lines per claim")(seed, claim, low, high, mix or None)


def _register() -> None:
    from sdg.engine.generators import register_algorithm

    register_algorithm("claims per member", 1, _claims_of_v1)
    register_algorithm("claims per member", 2, _claims_of_v2)
    register_algorithm("lines per claim", 1, _lines_of_v1)
    register_algorithm("members", 1, _members_v1)
    register_algorithm("members", 2, _members_v2)


from sdg.writers import adjudication  # noqa: E402,F401  (registers what a claim paid)
from sdg.writers.edi_formats import registry as _formats  # noqa: E402,F401  (registers shared loops)


_register()


def _total(lines: list[dict[str, Any]], element: str) -> str:
    """The sum of an element over a claim's lines, to the cent."""
    amounts = []
    for line in lines:
        try:
            amounts.append(Decimal(str(line.get(element) or "0").strip() or "0"))
        except InvalidOperation:
            continue
    return f"{sum(amounts, Decimal('0')):.2f}" if amounts else ""


def _files_setting(name: str, default: Any) -> Any:
    """A pack's files setting, or `default` where the pack has none."""
    return (packs.active().data.get("files") or {}).get(name, default)


def holds_no_names(records: list[dict[str, Any]]) -> bool:
    """Whether these records come from a store extracted without names: not
    one of them holds any (pack.json files.head_names.elements)."""
    names = (_files_setting("head_names", None) or {}).get("elements") or []
    return bool(names) and not any(r.get(e) not in (None, "") for r in records for e in names)


def _as_person(record: dict[str, Any], rules: dict[str, dict], seed: str, place: int,
               as_of: str, *, nameless: bool) -> dict[str, Any]:
    """A record from the store with the person's own details it does not hold
    made once, the same on every row written for it.

    Only for a store extracted without identifying fields (`nameless`, from
    holds_no_names over the whole store). A store that holds names is written
    exactly as it always was. Each missing element
    (files.person_elements) is drawn from its rule, seeded by the record's
    place. The household head's names are drawn once per household, seeded
    by the head's ID (files.head_names.key): a dependent's rows name the same
    head as each other, and as the head's own rows."""
    person = dict(record)
    head = _files_setting("head_names", None)
    if not head or not nameless:
        return person
    household = zlib.crc32(str(person.get(head["key"]) or f"place {place}").encode("utf-8"))
    own_head = _is_subscriber(person)
    for element in _files_setting("person_elements", []):
        if person.get(element) in (None, "") and element in rules:
            if own_head and element in head["elements"]:
                person[element] = _made(element, rules, f"{seed}:head", household, as_of)
            else:
                person[element] = _made(element, rules, f"{seed}:person", place, as_of, person)
    if not own_head:
        for element in head["elements"]:
            named = f"{head['prefix']}{element}"
            if person.get(named) in (None, "") and element in rules:
                person[named] = _made(element, rules, f"{seed}:head", household, as_of)
    return person


def _made(element: str, rules: dict[str, dict], seed: str, index: int,
          as_of: str, context: dict[str, Any] | None = None) -> str:
    """A claim's own value, never read off the member it belongs to.

    `context` is what the claim or line holds so far, which a combination is
    drawn to agree with.
    """
    rule = rules.get(element)
    if rule is None:
        return ""
    return rule_values.produce({**rule, "element": element}, seed=seed,
                               row_index=index, as_of=as_of, context=context)


# ---------------------------------------------------------------------------
# Filling in what a record does not hold
# ---------------------------------------------------------------------------

def complete(
    records: Iterable[dict[str, Any]],
    wanted: list[str],
    rules: dict[str, dict],
    *,
    seed: str,
    as_of: str = "2026-06-30",
) -> Iterator[dict[str, Any]]:
    """Each record with every wanted element present, drawn from its rule.

    Drawn at the record's position, counted from one, with the seed an EDI
    file uses -- so a CSV and an 834 of the same new members name the same
    people.
    """
    for position, record in enumerate(records, start=1):
        for element in wanted:
            if record.get(element) not in (None, ""):
                continue
            rule = rules.get(element)
            if rule is not None:
                record[element] = rule_values.produce(
                    {**rule, "element": element}, seed=seed,
                    row_index=position, as_of=as_of, context=record,
                )
        yield record


# ---------------------------------------------------------------------------
# The pattern store: members and claim structure without any target's plan
# ---------------------------------------------------------------------------

_KEYS = set(held("pack:files.record_keys"))


def _store_columns(shape, table_suffix: str) -> dict[str, str]:
    """Element to column, in the store extract whose name ends `table_suffix`."""
    from collections import Counter

    out: dict[str, str] = {}
    for element in shape.elements:
        found = [q.rpartition(".") for q in element.observed_in]
        here = [column for table, _, column in found if table.endswith(table_suffix)]
        if not here:
            continue
        # Where a table has several columns read as one element -- a product
        # code beside the class and subgroup IDs -- the one the other tables
        # also use for it is the element; the rest only looked like it.
        usual = Counter(column for _t, _, column in found)
        out[element.element] = max(here, key=lambda c: (usual[c], -here.index(c)))
    return out


def store_members(keys: list[Any], shape, registry, *, connection: str = "denorm",
                  table: str | None = None) -> list[dict[str, Any]]:
    """The primary records with these keys, read from the pattern store, keyed
    by element (health: members; a bank: customers).

    A pattern's records are people in the sources; the store holds them. Read
    here, a file from a pattern needs no target, onboarded or not. One row
    per record: the latest (pack.json files.latest_columns; health: the
    enrollment whose plan took effect last).
    """
    from sdg.analyzer import entity_loader

    if table is None:
        table = entity_loader.table(packs.active().primary)
    columns = _store_columns(shape, table.split(".")[-1])
    have = entity_loader.present_columns(table)
    # The store's own key of the record (health: MEME_CK; a bank: CUSTOMER_ID).
    key = packs.plain_name(held("features:store.member_key"), "features.json store.member_key")
    if key not in have:
        raise ValueError(f"the store has no {key} in {table}")
    # An ID the store holds none of its own, made the way the source makes it
    # (pack.json files.composed_id; health: the household head's ID and a
    # two-digit suffix, the way that source identifies a person).
    made = COMPOSED_ID or {}
    composed = (bool(made) and made["element"] not in columns
                and {made["from"], made["suffix"]} <= have)
    own = _own_columns(shape, table.split(".")[-1], key.split("_")[0] + "_")
    # A store extracted without names: none of them is a column it holds.
    names = (_files_setting("head_names", None) or {}).get("elements") or []
    nameless_store = bool(names) and not any(e in columns for e in names)
    named = sorted(set(columns.values()) | set(own.values()) | {key}
                   | ({made["from"], made["suffix"]} if composed else set()))
    select = ", ".join(f"[{c}]" for c in named)
    latest = next((c for c in LATEST_COLUMNS if c in have), None)
    order = f"[{latest}] DESC" if latest else f"[{key}]"
    wanted = [k for k in dict.fromkeys(keys) if k is not None]
    by_key: dict[Any, dict[str, Any]] = {}
    for start in range(0, len(wanted), 1000):
        piece = wanted[start:start + 1000]
        marks = ", ".join("?" for _ in piece)
        if latest is None:
            # Nothing says which of a record's rows is its latest: there must
            # be one row per record, or which one is read would be chance.
            twice = registry.query(
                connection,
                f"SELECT TOP 1 [{key}] AS k FROM {table} WHERE [{key}] IN ({marks}) "
                f"GROUP BY [{key}] HAVING COUNT(*) > 1",
                piece,
            )
            if twice:
                raise ValueError(
                    f"{table} holds more than one row for {key} {twice[0]['k']}, and the "
                    "business pack names no column telling which is the latest "
                    "(pack.json files.latest_columns).")
        rows = registry.query(
            connection,
            f"SELECT {select} FROM (SELECT {select}, ROW_NUMBER() OVER "
            f"(PARTITION BY [{key}] ORDER BY {order}) AS _rn FROM {table} "
            f"WHERE [{key}] IN ({marks})) AS m WHERE _rn = 1",
            piece,
        )
        for row in rows:
            record = {element: row.get(column) for element, column in columns.items()
                      if row.get(column) not in (None, "")}
            if composed and row.get(made["from"]) not in (None, ""):
                suffix = row.get(made["suffix"])
                record[made["element"]] = (f"{str(row[made['from']]).strip()}"
                                           f"{int(suffix) if suffix is not None else 0:02d}")
            if not _is_subscriber(record):
                _name_subscriber(record, row, own, composed, nameless_store)
            by_key[row.get(key)] = record
    return [by_key[k] for k in wanted if k in by_key]


# How a record's ID is made where the store holds none, and which of its rows
# is the latest (pack.json files; health: FACETS' member ID, the latest
# enrollment).
COMPOSED_ID: dict[str, str] | None = held("pack:files.composed_id")
LATEST_COLUMNS: list[str] = held("pack:files.latest_columns")


# A member who is the subscriber, as RELATIONSHIP_CODE says it: FACETS' M, and
# X12's 18, which the 834 writes for it.
SUBSCRIBER = set(held_or("pack:files.subscriber_codes", set()))


def _is_subscriber(record: dict[str, Any]) -> bool:
    code = record.get("RELATIONSHIP_CODE")
    return code in (None, "") or str(code).strip().upper() in SUBSCRIBER


def _own_columns(shape, table_suffix: str, prefix: str) -> dict[str, str]:
    """Elements a table holds twice, with the member's own column for each: a
    first name in MEME_FIRST_NAME beside the subscriber's in SBSB_FIRST_NAME.
    The member's is the one named like the member key."""
    out: dict[str, str] = {}
    for element in shape.elements:
        here = [c for t, _, c in (q.rpartition(".") for q in element.observed_in)
                if t.endswith(table_suffix)]
        mine = [c for c in here if c.startswith(prefix)]
        if len(here) > 1 and mine:
            out[element.element] = mine[0]
    return out


def _name_subscriber(record: dict[str, Any], row: dict[str, Any],
                     own: dict[str, str], composed: bool, nameless_store: bool = False) -> None:
    """A dependent's record, naming the dependent and their subscriber.

    Where the store holds a name twice, the member's own column is the
    patient's and the other is the subscriber's, kept as SUBSCRIBER_<element>
    -- an 837 writes the subscriber, then the patient. A subscriber's own
    record is left as it is, so their claims read as they always have."""
    for element, column in own.items():
        theirs, mine = record.get(element), row.get(column)
        if theirs not in (None, ""):
            # Kept even when it is the same -- a child shares a last name.
            record[f"SUBSCRIBER_{element}"] = theirs
        if mine not in (None, ""):
            record[element] = mine
    # The household head's own ID, as the composed ID numbers the head
    # (pack.json files.composed_id.subscriber_suffix: health's 00): where the
    # store names the head, or holds no names at all (claims_for then draws
    # the head's names, and the head is still written with this ID).
    head = _files_setting("head_names", None) or {}
    names = head.get("elements") or []
    if nameless_store or any(f"{head['prefix']}{e}" in record for e in names):
        made = COMPOSED_ID or {}
        record["SUBSCRIBER_MEMBER_ID"] = (
            f"{str(row[made['from']]).strip()}{made['subscriber_suffix']}"
            if composed and row.get(made.get("from", ""))
            else record.get("SUBSCRIBER_ID") or "")


def store_claim_elements(shape) -> tuple[list[str], list[str]]:
    """What a child record carries once and what each of its lines carries,
    from the store (health: a claim and its lines; a bank: an account and its
    transactions), in the pack's loaders' tables.

    The primary record's own elements are neither: it exists and is read.
    """
    from sdg.analyzer import entity_loader

    pack = packs.active()
    member_side = set(_store_columns(shape, entity_loader.table(pack.primary).split(".")[-1]))
    claims = _store_columns(shape, entity_loader.table(pack.child).split(".")[-1])
    line = [e for e in shape.line_elements if e not in _KEYS]
    claim = [e for e in claims if e not in _KEYS and e not in member_side and e not in line]
    return claim, line
