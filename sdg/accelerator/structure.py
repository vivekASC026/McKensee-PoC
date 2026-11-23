"""Denorm structures: the tables the extract fills, one per record kind.

Each record kind gets one `<many>_denorm` table: the primary at its record
grain, the child at its lines grain when it has lines. A table holds the
record's key (the child's also the key it joins the primary on), the lines'
own key, each selected level's key and label, each selected feature, the
dates a derived feature needs, and the columns the person added. Column
names stay the source's, so a configuration written from the plan names
them as the source does; two columns of one name from different tables are
told apart by the second's table name in front, and a column named with a
word SQL reserves (KEY, ORDER, USER: reserved.py) takes its table's name in
front too, since the loaders write names unbracketed. Types come from the
model.

Every column has a privacy class, proposed by `privacy_for`:
- an identifying personal column (a name, a national number, a contact, a
  street) is withheld and locked: it can never be made kept. When the two
  tables join on it, it is a keyed surrogate instead (nvarchar(16),
  SDG_EXTRACT_KEY), still locked;
- a birth date is kept only as the input of a selected derived feature (an
  age), and a postal code, city or state is kept: both by the approved
  decision that pattern data keeps them. A birth date serving anything else
  is withheld and locked;
- a key (the record's, the lines', or one the two tables join on) whose name
  reads as a person, an account or a contact, or that was never profiled,
  may say who a row is about: proposed as a keyed surrogate (on both sides
  of the join when the tables join on it), not locked. The person may keep
  it only by confirming it holds nothing personal (`confirmed_not_personal`,
  set by an explicit edit), which the build's privacy check honors;
- everything else is kept, except a column the person added whose name or
  many distinct values may say who a row is about: proposed withheld, kept
  only once the person confirms it holds nothing personal, as for a key.
The person's own choices are kept by (table, column) whenever the structure
is proposed again; a locked column is never set kept.

A level reached down into a link table (a bridge) holds several link rows
per record. With a start and end date, the row in force today with the
latest start is picked; without one, the record repeats once per link row,
and the level's reason says so. The extract refuses such a table until the
person accepts the grain change (plan.accept_grain).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sdg import packs
from sdg.accelerator import criteria
from sdg.accelerator.graph import KeyGraph
from sdg.accelerator.hierarchy import BIRTH_ELEMENT, Join, LevelCandidate
from sdg.accelerator.landscape import DENY_TOKENS, PERSONAL_CLASSES, SHORT_VALUE
from sdg.accelerator.reserved import is_reserved
from sdg.accelerator.tables import ROLES

if TYPE_CHECKING:
    from sdg.accelerator.plan import SourcePlan

SERVES = ("key", "lines_key", "level_key", "level_label", "feature", "derive_input", "extra")
PRIVACY = ("kept", "withheld", "surrogate")
GRAINS = ("record", "lines")
SURROGATE_TYPE = "nvarchar(16)"
SUFFIX = "_denorm"
# Places a pattern keeps by decision: where a person lives, to the town.
PLACE_ELEMENTS = frozenset({"ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE"})
# A column the person adds with this many distinct values per row, or more,
# may single a row out.
EXTRA_DISTINCT_RATIO = 0.5
NAME_MAX = 100

_NOT_PLAIN = re.compile(r"[^A-Za-z0-9_]+")


@dataclass
class DenormColumn:
    name: str
    source_table: str
    source_column: str
    sql_type: str
    serves: str
    privacy: str
    proposed: str
    reason: str
    locked: bool = False
    # Whether the two denorm tables join on this column.
    joins: bool = False
    # The person confirmed, by an explicit edit, that a key proposed as a
    # surrogate (for its name or a missing profile) holds nothing personal.
    confirmed_not_personal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "DenormColumn":
        try:
            column = cls(**held)
        except TypeError as exc:
            raise ValueError(f"a saved denorm column is not one this version reads: {exc}") \
                from exc
        if column.serves not in SERVES or column.privacy not in PRIVACY \
                or column.proposed not in PRIVACY \
                or not isinstance(column.confirmed_not_personal, bool):
            raise ValueError(f"a saved denorm column has an unknown use or privacy class: "
                             f"{column.source_table}.{column.source_column}")
        return column


@dataclass
class DenormTable:
    role: str
    name: str
    grain: str
    columns: list[DenormColumn] = field(default_factory=list)
    # The bridges picked: {table, start, end}, the row in force today with
    # the latest start.
    pick: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "grain": self.grain,
                "columns": [c.to_dict() for c in self.columns],
                "pick": [dict(p) for p in self.pick]}

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "DenormTable":
        return cls(role=held["role"], name=held["name"], grain=held["grain"],
                   columns=[DenormColumn.from_dict(c) for c in held.get("columns") or []],
                   pick=[dict(p) for p in held.get("pick") or []])


# --- names ----------------------------------------------------------------------

def slug(words: str) -> str:
    """Words as a plain lowercase name: runs of anything but letters and
    digits become one underscore; a leading digit gets t_ in front."""
    plain = _NOT_PLAIN.sub("_", (words or "").strip().lower()).strip("_")
    plain = re.sub(r"_+", "_", plain)[:NAME_MAX].strip("_")
    if plain and plain[0].isdigit():
        plain = "t_" + plain
    return plain


def table_name(plan: "SourcePlan", role: str, graph: KeyGraph) -> str:
    """`<plain slug of the record kind's many name>_denorm`; the record
    table's own name while the kind is unnamed."""
    kind = plan.record_kinds[role] or {}
    many = (kind.get("name") or {}).get("many") or ""
    base = slug(many)
    if not base:
        found = graph.table(kind["table"])
        base = slug(found.table_name if found is not None else kind["table"]) or role
    return packs.plain_name(base + SUFFIX, f"the {role} denorm table")


def _short(graph: KeyGraph, table: str) -> str:
    found = graph.table(table)
    return found.table_name if found is not None else table.rsplit(".", 1)[-1]


def _plain_column(name: str) -> str:
    if packs._PLAIN_NAME.fullmatch(name):
        return name
    plain = _NOT_PLAIN.sub("_", name).strip("_") or "column"
    return ("c_" + plain if plain[0].isdigit() else plain)[:NAME_MAX]


# --- privacy --------------------------------------------------------------------

def _element(graph: KeyGraph, table: str, column: str) -> tuple[str | None, str | None]:
    """The base catalog element the column maps to, and its privacy class."""
    name = graph.element_of(table, column)
    element = graph._base_elements().get(name or "")
    return name, (element.pii if element is not None else None)


DENY_SAID = "its name reads as a person, an account or a contact"
NOT_PROFILED_SAID = "it was not profiled, so nothing is known of its values"
KEY_SERVES = ("key", "lines_key")
# What analysis groups and measures by: a name that reads as a person, an
# account or a contact is not kept unasked here either (a level's key becomes
# a keyed surrogate, so the level still groups; a label or a feature is
# withheld). A derived feature's input is not: it must be stored for the
# value derived from it.
NAMED_SERVES = ("level_key", "level_label", "feature")


def _deny_hit(column: str) -> bool:
    upper = column.upper()
    return any(token in upper for token in DENY_TOKENS)


def risky_key(graph: KeyGraph, table: str, column: str) -> str | None:
    """Why a key may say who a row is about; None when nothing does. Fails
    closed: a key whose name reads as a person, an account or a contact, or
    one never profiled, is taken as risky."""
    if _deny_hit(column):
        return DENY_SAID
    held = graph.profile_of(table, column)
    if held is None or not held.row_count:
        return NOT_PROFILED_SAID
    return None


def _key_like(serves: str, joins: bool) -> bool:
    return serves in KEY_SERVES or joins


def _risky_extra(graph: KeyGraph, table: str, column: str) -> str | None:
    """Why a column the person adds may say who a row is about; None when
    nothing does. Fails closed: a column never profiled, or free text longer
    than a short code, is taken as risky."""
    if _deny_hit(column):
        return DENY_SAID
    found = graph.table(table)
    col = found.column(column) if found is not None else None
    if col is None or col.is_date:
        return None
    if col.is_character:
        length = col.character_length()
        if length is None or length > SHORT_VALUE:
            return "free text, longer than a short code"
    held = graph.profile_of(table, column)
    if held is None or not held.row_count:
        return NOT_PROFILED_SAID
    if held.cardinality == "unique" or held.distinct_count / held.row_count >= EXTRA_DISTINCT_RATIO:
        return f"{held.distinct_count:,} distinct values in {held.row_count:,} rows"
    return None


def privacy_for(graph: KeyGraph, table: str, column: str, serves: str, *,
                joins: bool = False) -> tuple[str, str, bool]:
    """(privacy, reason, locked) for one column serving as `serves`. `joins`
    when the two denorm tables join on it. Locked means it can never be kept."""
    element, pii = _element(graph, table, column)
    if pii in PERSONAL_CLASSES:
        if joins:
            return ("surrogate", f"personal ({element}), and the two tables join on it: a "
                                 f"keyed surrogate stands in its place", True)
        if element == BIRTH_ELEMENT:
            if serves == "derive_input":
                return ("kept", "a birth date, kept only as the input of the age feature "
                                "(pattern data keeps birth dates, by decision)", False)
            return ("withheld", "a birth date is kept only as the input of an age feature",
                    True)
        if element in PLACE_ELEMENTS:
            return ("kept", f"a place ({element}): pattern data keeps postal code, city and "
                            f"state, by decision", False)
        return ("withheld", f"identifies a person ({element}): withheld, and never kept", True)
    said = {"key": "the record's key", "lines_key": "the lines' own key",
            "level_key": "a level's key", "level_label": "a level's label",
            "feature": "a feature", "derive_input": "the input of a derived feature"}
    if _key_like(serves, joins):
        risky = risky_key(graph, table, column)
        if risky:
            both = ", on both sides of the join" if joins else ""
            return ("surrogate", f"{said.get(serves, 'a key')}; {risky}: a keyed surrogate "
                                 f"stands in its place{both}. Keep it only by confirming it "
                                 f"holds nothing personal", False)
    if serves in NAMED_SERVES and not joins and _deny_hit(column):
        if serves == "level_key":
            return ("surrogate", f"a level's key; {DENY_SAID}: a keyed surrogate stands in its "
                                 f"place, so the level still groups. Keep it only by confirming "
                                 f"it holds nothing personal", False)
        return ("withheld", f"{said[serves]}; {DENY_SAID}: withheld until the person confirms "
                            f"it holds nothing personal", False)
    if serves == "extra":
        risky = _risky_extra(graph, table, column)
        if risky:
            return ("withheld", f"added by the person; {risky}: withheld until the person "
                                f"confirms it holds nothing personal", False)
        return ("kept", "added by the person", False)
    return ("kept", said.get(serves, serves) + (", joining the two tables" if joins else ""),
            False)


def refusal(column: DenormColumn, privacy: str, graph: KeyGraph, *,
            confirmed: bool | None = None) -> str | None:
    """Why a column may not take `privacy`; None when it may. A key that may
    say who a row is about (risky_key) is kept only once the person confirmed
    it holds nothing personal: `confirmed` when given, else the column's own
    confirmed_not_personal."""
    if privacy not in PRIVACY:
        return f"{privacy!r} is not a privacy class: use {', '.join(PRIVACY)}"
    where = f"{column.source_table}.{column.source_column}"
    if privacy == "kept" and column.locked:
        return f"{where} is locked: {column.reason}"
    if privacy == "kept" and _key_like(column.serves, column.joins):
        sure = column.confirmed_not_personal if confirmed is None else confirmed
        risky = risky_key(graph, column.source_table, column.source_column)
        if risky and not sure:
            return (f"{where} may say who a row is about ({risky}): confirm it holds nothing "
                    f"personal to keep it, or keep it a surrogate")
    if privacy == "kept" and column.serves in NAMED_SERVES and not column.joins:
        sure = column.confirmed_not_personal if confirmed is None else confirmed
        if _deny_hit(column.source_column) and not sure:
            return (f"{where} may say who a row is about ({DENY_SAID}): confirm it holds "
                    f"nothing personal to keep it")
    if privacy == "kept" and column.serves == "extra":
        sure = column.confirmed_not_personal if confirmed is None else confirmed
        risky = _risky_extra(graph, column.source_table, column.source_column)
        if risky and not sure:
            return (f"{where} was added by the person and may say who a row is about "
                    f"({risky}): confirm it holds nothing personal to keep it")
    if privacy == "withheld" and column.joins:
        return f"the two tables join on {where}: keep it or make it a surrogate"
    if privacy == "withheld" and column.serves in ("key", "lines_key"):
        return f"{where} is the table's key: keep it or make it a surrogate"
    if privacy == "withheld" and column.serves == "derive_input":
        return (f"{where} is a derived feature's input: deselect the feature instead, and the "
                f"column leaves the table")
    found = graph.table(column.source_table)
    source = found.column(column.source_column) if found is not None else None
    if source is None:
        return f"the source has no column {where}"
    if privacy == "surrogate" and source.is_date:
        return f"{where} is a date: a surrogate stands for a key or a code"
    return None


# --- proposing ------------------------------------------------------------------

def stated_to_primary(plan: "SourcePlan") -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    """The joins the person stated from the child's record to the primary's,
    as (child columns, primary columns)."""
    primary = (plan.record_kinds.get("primary") or {}).get("table")
    child = (plan.record_kinds.get("child") or {}).get("table")
    return {(tuple(j.from_columns), tuple(j.to_columns))
            for j in plan.joins.get("stated", [])
            if j.from_table == child and j.to_table == primary}


def primary_edges(plan: "SourcePlan", graph: KeyGraph) -> list[Any]:
    """Every edge from the child's record table to the primary's, the one the
    two tables join on first: one the person stated (in the graph or the
    plan), then a declared one, then by columns."""
    primary = (plan.record_kinds.get("primary") or {}).get("table")
    child = (plan.record_kinds.get("child") or {}).get("table")
    if not primary or not child:
        return []
    stated = stated_to_primary(plan)

    def rank(e: Any) -> tuple:
        named = e.kind == "stated" or (tuple(e.child_columns), tuple(e.parent_columns)) in stated
        return (0 if named else {"declared": 1}.get(e.kind, 2), e.child_columns)

    return sorted((e for e in graph.edges_from(child) if e.parent_table == primary), key=rank)


def _join_columns(plan: "SourcePlan", graph: KeyGraph) -> tuple[list[str], list[str]]:
    """The primary's columns and the child's that the two tables join on;
    none while there is no child keyed to the primary."""
    edges = primary_edges(plan, graph)
    if not edges:
        return [], []
    return list(edges[0].parent_columns), list(edges[0].child_columns)


def join_pairs(plan: "SourcePlan", graph: KeyGraph) -> list[tuple[tuple[str, str],
                                                                  tuple[str, str]]]:
    """Each pair of columns the two denorm tables join on: (primary table,
    column) with (child table, column)."""
    parent, child = _join_columns(plan, graph)
    if not parent:
        return []
    primary = plan.record_kinds["primary"]["table"]
    record = plan.record_kinds["child"]["table"]
    return [((primary, p), (record, c)) for p, c in zip(parent, child)]


def partners(plan: "SourcePlan", graph: KeyGraph, table: str,
             column: str) -> list[tuple[str, str]]:
    """The column on the other side of the join, when the two tables join on
    this one; an empty list otherwise."""
    out = []
    for a, b in join_pairs(plan, graph):
        if (table, column) == a:
            out.append(b)
        elif (table, column) == b:
            out.append(a)
    return out


def _join_consistent(tables: list[DenormTable], plan: "SourcePlan", graph: KeyGraph) -> None:
    """Both sides of a join hold one class, or the join finds nothing: when
    either side is personal both are surrogates, locked; when either side's
    name or missing profile proposed a surrogate (risky_key), both are
    proposed so; when the person made either a surrogate, both are."""
    for a, b in join_pairs(plan, graph):
        held = find(tables, *a) + find(tables, *b)
        personal = [side for side in (a, b) if graph.is_personal(*side)]
        if not personal and not any(c.privacy == "surrogate" for c in held):
            continue
        risky = next((c for c in held if c.proposed == "surrogate" and not c.locked), None)
        for column in held:
            if personal and not column.locked:
                other = ".".join(personal[0])
                column.proposed = "surrogate"
                column.locked = True
                column.confirmed_not_personal = False
                column.reason = (f"the two tables join on it, and {other} is personal: a "
                                 f"keyed surrogate on both sides")
            elif not personal and risky is not None and column.proposed != "surrogate":
                column.proposed = "surrogate"
                column.reason = (f"the two tables join on it, and "
                                 f"{risky.source_table}.{risky.source_column} may say who a "
                                 f"row is about: a keyed surrogate on both sides. Keep them "
                                 f"only by confirming they hold nothing personal")
            column.privacy = "surrogate"
            column.sql_type = SURROGATE_TYPE


def _down(graph: KeyGraph, join: Join) -> bool:
    return any(e.child_table == join.to_table and e.parent_table == join.from_table
               and e.child_columns == join.to_columns for e in graph.edges_to(join.from_table))


def _role_tables(plan: "SourcePlan", role: str) -> set[str]:
    return {t.table for t in plan.tables if any(s["role"] == role for s in t.serves)}


def _wanted(plan: "SourcePlan", role: str, graph: KeyGraph,
            extras: list[tuple[str, str]]) -> tuple[list[tuple], list[dict], dict[str, str]]:
    """(table, column, serves, joins) in order, the bridges picked, and what a
    column's reason adds (by (table, column)) where a bridge repeats records:
    a level's key, or a column the person added from that bridge."""
    kind = plan.record_kinds[role]
    record, lines = kind["table"], kind.get("lines_table")
    keys = list(kind.get("key_columns") or [])
    parent_join, child_join = _join_columns(plan, graph)
    joining = parent_join if role == "primary" else child_join
    wanted: list[tuple] = []
    for column in keys:
        wanted.append((record, column, "key", column in joining))
    if role == "primary":
        # The child may key to columns other than the primary's own key.
        for column in parent_join:
            if column not in keys:
                wanted.append((record, column, "key", True))
    if role == "child":
        for column in child_join:
            wanted.append((record, column, "key", True))
    if lines:
        found = graph.table(lines)
        for column in (found.primary_key if found is not None else []):
            if column not in keys:
                wanted.append((lines, column, "lines_key", False))
    picks: list[dict] = []
    repeats: dict[tuple[str, str], list[str]] = {}
    # Link tables entered downward with no dates to pick a row by.
    unpicked: set[str] = set()
    one = ((kind.get("name") or {}).get("one") or "record")
    levels: list[LevelCandidate] = sorted((lv for lv in plan.levels(role) if lv.selected),
                                          key=lambda lv: lv.order)
    for level in levels:
        wanted.append((level.table, level.column, "level_key", False))
        if level.label_table and level.label_column:
            wanted.append((level.label_table, level.label_column, "level_label", False))
        for join in level.path:
            if not _down(graph, join):
                continue
            pair = criteria.best_pair(graph, join.to_table)
            if pair is not None:
                pick = {"table": join.to_table, "start": pair["start"], "end": pair["end"]}
                if pick not in picks:
                    picks.append(pick)
            else:
                unpicked.add(join.to_table)
                said = level.label_column or level.column
                repeats.setdefault((level.table, level.column), []).append(
                    f"{join.to_table} links without dates: each {one} may appear once per "
                    f"{said}, so the table's grain grows; accept the grain change to build "
                    f"it, or deselect the level")
    for feature in plan.features(role):
        if not feature.selected:
            continue
        derive = feature.derive or {}
        if derive.get("from"):
            wanted.append((record, derive["from"], "derive_input", False))
        else:
            wanted.append((feature.table or record, feature.column, "feature", False))
    for table, column in extras:
        wanted.append((table, column, "extra", False))
        if table in unpicked:
            repeats.setdefault((table, column), []).append(
                f"{table} links without dates: each {one} may appear once per row of it, so "
                f"the table's grain grows; accept the grain change to build it, or deselect "
                f"the level through it")
    return wanted, picks, {k: "; ".join(v) for k, v in repeats.items()}


def unpicked_links(plan: "SourcePlan", role: str, graph: KeyGraph) -> list[str]:
    """The link tables the role's selected levels enter downward with no
    dates to pick a row by: each repeats the role's records once per row."""
    out: list[str] = []
    for level in plan.levels(role):
        if not level.selected:
            continue
        for join in level.path:
            if _down(graph, join) and criteria.best_pair(graph, join.to_table) is None \
                    and join.to_table not in out:
                out.append(join.to_table)
    return out


def _one_table(plan: "SourcePlan", role: str, graph: KeyGraph, name: str,
               previous: "DenormTable | None", choices: dict[tuple[str, str], str],
               confirmed: set[tuple[str, str]] | None = None) -> DenormTable:
    kind = plan.record_kinds[role]
    extras = []
    if previous is not None:
        allowed = _role_tables(plan, role)
        extras = [(c.source_table, c.source_column) for c in previous.columns
                  if c.serves == "extra" and c.source_table in allowed]
    wanted, picks, repeats = _wanted(plan, role, graph, extras)
    taken: dict[str, tuple[str, str]] = {}
    seen: set[tuple[str, str]] = set()
    out: list[DenormColumn] = []
    for table, column, serves, joins in wanted:
        if (table, column) in seen:
            continue
        found = graph.table(table)
        source = found.column(column) if found is not None else None
        if source is None:
            continue
        seen.add((table, column))
        privacy, reason, locked = privacy_for(graph, table, column, serves, joins=joins)
        if role == "child" and joins and privacy == "kept":
            reason = "the key to the primary record, joining the two tables"
        if (table, column) in repeats:
            reason += "; " + repeats[(table, column)]
        base = _plain_column(column)
        named = base
        if is_reserved(named):
            # The loaders write column names unbracketed: a reserved word
            # takes its table's short name in front, as a clash does.
            named = _plain_column(f"{_short(graph, table)}_{column}")
            number = 2
            while named.upper() in taken or is_reserved(named):
                named, number = f"{_plain_column(f'{_short(graph, table)}_{column}')}_{number}", \
                    number + 1
            reason += f"; named {named}, since {base} is a word SQL reserves"
        elif named.upper() in taken:
            named = _plain_column(f"{_short(graph, table)}_{column}")
            number = 2
            while named.upper() in taken:
                named, number = f"{_plain_column(f'{_short(graph, table)}_{column}')}_{number}", \
                    number + 1
            reason += f"; named {named}, since {base} is taken by " \
                      f"{'.'.join(taken[base.upper()])}"
        elif named != column:
            reason += f"; named {named}, a plain name for {column}"
        packs.plain_name(named, f"{name} column")
        taken[named.upper()] = (table, column)
        held = DenormColumn(
            name=named, source_table=table, source_column=column,
            sql_type=SURROGATE_TYPE if privacy == "surrogate" else source.type_description(),
            serves=serves, privacy=privacy, proposed=privacy, reason=reason, locked=locked,
            joins=joins)
        held.confirmed_not_personal = (table, column) in (confirmed or set())
        chosen = choices.get((table, column))
        if chosen is not None and chosen != privacy and refusal(held, chosen, graph) is None:
            held.privacy = chosen
            held.sql_type = SURROGATE_TYPE if chosen == "surrogate" else source.type_description()
        out.append(held)
    return DenormTable(role=role, name=name, grain="lines" if kind.get("lines_table") else "record",
                       columns=out, pick=picks)


def propose(plan: "SourcePlan", graph: KeyGraph,
            previous: list[DenormTable] | None = None) -> list[DenormTable]:
    """One denorm table per record kind. With `previous` (the structure as
    it stood), the columns the person added stay while their table is still
    one the record kind reads, and every privacy the person chose stays by
    (table, column) where it is still allowed."""
    previous = previous or []
    choices = {(c.source_table, c.source_column): c.privacy
               for t in previous for c in t.columns if c.privacy != c.proposed}
    # A confirmation holds while the column is kept: made a surrogate again,
    # it is asked for again.
    confirmed = {(c.source_table, c.source_column) for t in previous for c in t.columns
                 if c.confirmed_not_personal and c.privacy == "kept"}
    out: list[DenormTable] = []
    names: set[str] = set()
    for role in ROLES:
        if not plan.record_kinds.get(role):
            continue
        name = table_name(plan, role, graph)
        if name in names:
            name = packs.plain_name(name[:-len(SUFFIX)] + f"_{role}" + SUFFIX,
                                    f"the {role} denorm table")
        names.add(name)
        before = next((t for t in previous if t.role == role), None)
        out.append(_one_table(plan, role, graph, name, before, choices, confirmed))
    _join_consistent(out, plan, graph)
    return out


def find(structure: list[DenormTable], table: str, column: str) -> list[DenormColumn]:
    return [c for t in structure for c in t.columns
            if c.source_table == table and c.source_column == column]
