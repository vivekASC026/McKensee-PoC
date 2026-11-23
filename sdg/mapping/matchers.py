"""Evidence that a column is a particular business element.

Four kinds, each answering a different question.

    alias        does the column's name contain an alias the catalog records?
                 Matched on tokens, so an alias of FIRST_NAME covers
                 MEME_FIRST_NAME without anyone writing that down.
    format       do the column's values match the element's format? Support
                 only: a subgroup id and a product code can share a shape, and
                 a shape is never the reason a column is an element.
    check digit  do the values satisfy the element's check-digit scheme? Almost
                 nothing passes an NPI check by accident.
    propagation  is the column a foreign key to a column already mapped? Then
                 it holds the same element by construction.

A name-token overlap is also recorded, but only as support for another kind:
a column that shares one word with an element and nothing else is not that
element. LAST_UPD_TS shares LAST with LAST_NAME and holds dates.

No element is offered to a column that cannot hold its values: a date to a
date or time column, a number to a number column, names and codes to text --
or to a number column, for a code made only of digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from sdg.catalog.model import Catalog, Element
from sdg.discovery.classify import tokenize
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.profiling.format import matches_format
from sdg.profiling.model import ColumnProfile

# Tokens that say nothing about what a column holds. An alias made only of
# these would match half the schema.
GENERIC_TOKENS = {
    "ID", "CK", "CD", "NO", "NUM", "KEY", "CODE", "SK", "NBR", "SEQ",
    "DT", "TS", "FLAG", "IND", "TYPE", "NAME", "VAL", "AMT", "QTY",
}

FORMAT_MATCH_MIN_COVERAGE = 0.98

# Evidence that can make a column an element. The rest only adds weight.
NAMING_KINDS = {"alias", "fk_propagation", "check_digit"}

# A format shared by a dozen kinds of code -- two letters, one digit, five
# digits -- says nothing on its own. One this long, or carrying letters of its
# own (PRV, GRP), does.
DISTINCTIVE_FORMAT_LENGTH = 6
DISTINCTIVE_FORMAT_LITERALS = 2

# One piece of a format -- \d, a class like [A-Z], an escaped or a plain
# character -- and how many times it repeats.
_FORMAT_TOKEN = re.compile(r"(\\[dw.]|\\.|\[[^\]]*\]|[^\\\[])(\{(\d+)\}|\{\d*,\d*\}|[+*?])?")


def format_literals(regex: str | None) -> int:
    """How many fixed letters and digits a format spells out: the CUST of CUST000012."""
    literals = 0
    for match in _FORMAT_TOKEN.finditer(regex or ""):
        atom, repeat, times = match.group(1), match.group(2), match.group(3)
        if atom.isalnum() and (not repeat or times):
            literals += int(times) if times else 1
    return literals


def format_is_distinctive(regex: str | None) -> bool:
    """Whether values matching `regex` could be little else."""
    if not regex:
        return False
    length, literals, position = 0, 0, 0
    for match in _FORMAT_TOKEN.finditer(regex):
        if match.start() != position:
            return False
        position = match.end()
        atom, repeat, times = match.group(1), match.group(2), match.group(3)
        if repeat and times is None:
            return literals >= DISTINCTIVE_FORMAT_LITERALS
        count = int(times) if times else 1
        length += count
        if atom.isalnum():
            literals += count
    if position != len(regex):
        return False
    return length >= DISTINCTIVE_FORMAT_LENGTH or literals >= DISTINCTIVE_FORMAT_LITERALS


def _value_kinds(element: Element) -> set[str]:
    """The kinds of column an element's values can go in."""
    generator = element.generator
    if generator == "date_relative":
        return {"date"}
    if generator in {"number", "sequence"}:
        return {"number"}
    if generator == "pattern":
        regex = str(element.params.get("regex") or element.format or "")
        digits_only = bool(regex) and re.fullmatch(r"(\\d|\[0-9\]|\[1-9\]|\{\d+(,\d*)?\})+", regex)
        return {"text", "number"} if digits_only else {"text"}
    if generator == "lookup":
        values = element.params.get("values") or []
        field = element.params.get("field")
        plain = [v.get(field) if isinstance(v, dict) and field else v for v in values]
        if plain and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in plain):
            return {"number"}
        return {"text"}
    if generator == "constant":
        value = element.params.get("value")
        return {"number"} if isinstance(value, (int, float)) else {"text"}
    # Reused or copied from elsewhere: a code or a name, never a date.
    return {"text", "number"}


def _column_kind(column: Column) -> str:
    if column.is_date:
        return "date"
    if column.is_numeric:
        return "number"
    if column.is_character:
        return "text"
    return "other"


def can_hold(column: Column, element: Element) -> bool:
    """Whether the column's type can hold the element's values at all."""
    return _column_kind(column) in _value_kinds(element)


@dataclass
class Evidence:
    kind: str
    detail: str
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "score": round(self.score, 4)}


@dataclass
class Match:
    element: str
    evidence: list[Evidence] = field(default_factory=list)

    def kinds(self) -> set[str]:
        return {e.kind for e in self.evidence}

    def score(self) -> float:
        return round(sum(e.score for e in self.evidence), 4)


def informative(tokens: Iterable[str]) -> set[str]:
    """Tokens that carry meaning, with the generic key words removed."""
    return {t for t in tokens if t not in GENERIC_TOKENS}


def alias_match(column_name: str, element: Element) -> Evidence | None:
    """An alias matches when all of its tokens appear in the column's name.

    Requiring a non-generic token stops an alias like SEQ_NO from matching
    every sequence column in the schema.
    """
    column_tokens = set(tokenize(column_name))
    best: tuple[int, str] | None = None
    for alias in element.all_names():
        alias_tokens = set(tokenize(alias))
        if not alias_tokens or not informative(alias_tokens):
            continue
        if alias_tokens <= column_tokens:
            weight = len(informative(alias_tokens))
            if best is None or weight > best[0]:
                best = (weight, alias)
    if best is None:
        return None
    weight, alias = best
    exact = set(tokenize(alias)) == set(tokenize(column_name))
    return Evidence(
        kind="alias",
        detail=(
            f"column name contains the alias {alias}"
            + ("" if not exact else ", exactly")
        ),
        score=1.0 if exact else 0.85 + 0.03 * weight,
    )


def name_token_match(column_name: str, element: Element,
                     table_name: str = "") -> Evidence | None:
    """A weak signal: meaningful tokens shared with the element or its aliases.

    A table's own prefix is not one: every CLCL column shares CLCL with an
    alias like CLCL_ME_AGE, and says nothing by it."""
    column_tokens = informative(tokenize(column_name)) - set(tokenize(table_name))
    if not column_tokens:
        return None
    best: tuple[int, str] | None = None
    for name in element.all_names():
        shared = column_tokens & informative(tokenize(name))
        if shared and (best is None or len(shared) > best[0]):
            best = (len(shared), ", ".join(sorted(shared)))
    if best is None:
        return None
    count, shared = best
    return Evidence(
        kind="name_token",
        detail=f"name shares {shared} with {element.name}",
        score=0.2 * count,
    )


def format_match(profile: ColumnProfile | None, element: Element) -> Evidence | None:
    """The column's fitted pattern equals the element's, or is covered by it."""
    if profile is None or not element.format:
        return None

    if not format_is_distinctive(element.format):
        return None

    fitted = profile.format_regex()
    if fitted and fitted == element.format:
        return Evidence(
            kind="format",
            detail=f"fitted format {fitted} equals the element's",
            score=0.6,
        )

    # Not identical, but the element's pattern may still cover the values.
    if fitted:
        sample = _sample_values(profile)
        if sample:
            coverage = matches_format(sample, element.format)
            if coverage >= FORMAT_MATCH_MIN_COVERAGE:
                return Evidence(
                    kind="format",
                    detail=(
                        f"{coverage:.0%} of sampled values match the element's "
                        f"format {element.format}"
                    ),
                    score=0.45,
                )
    return None


def _sample_values(profile: ColumnProfile) -> list[str]:
    """Values kept on the profile that a format can be tested against."""
    values: list[str] = []
    for top in profile.top_values or []:
        values.append(str(top.value).rstrip())
    for bound in (profile.minimum, profile.maximum):
        if isinstance(bound, str):
            values.append(bound.rstrip())
    return values


def check_digit_match(profile: ColumnProfile | None, element: Element) -> Evidence | None:
    """The values satisfy the check-digit scheme the element declares."""
    if profile is None or not element.check_digit or not profile.check_digit:
        return None
    if profile.check_digit.get("scheme") != element.check_digit:
        return None
    share = float(profile.check_digit.get("share") or 0.0)
    return Evidence(
        kind="check_digit",
        detail=(
            f"{share:.0%} of sampled values pass the {element.check_digit} "
            f"check-digit test"
        ),
        score=0.9,
    )


def propagation_match(
    table: Table,
    column: Column,
    model: PhysicalModel,
    already_mapped: dict[str, str],
) -> tuple[str, Evidence] | None:
    """A foreign key inherits the element its parent column carries."""
    foreign_key = table.foreign_key_columns().get(column.name)
    if foreign_key is None or len(foreign_key.columns) != 1:
        return None
    parent = model.table(foreign_key.parent_table)
    if parent is None:
        return None
    parent_column = foreign_key.parent_columns[0]
    element_name = already_mapped.get(f"{parent.name}.{parent_column}")
    if not element_name:
        return None
    kind = "inferred " if foreign_key.kind == "inferred" else ""
    return element_name, Evidence(
        kind="fk_propagation",
        detail=(
            f"{kind}foreign key to {parent.name}.{parent_column}, which maps to "
            f"{element_name}"
        ),
        score=0.95,
    )


def candidates_for(
    table: Table,
    column: Column,
    catalog: Catalog,
    profile: ColumnProfile | None,
    model: PhysicalModel,
    already_mapped: dict[str, str],
) -> list[Match]:
    """Every element with any evidence behind it, best first."""
    matches: dict[str, Match] = {}

    def add(element_name: str, evidence: Evidence | None) -> None:
        if evidence is None:
            return
        matches.setdefault(element_name, Match(element=element_name)).evidence.append(evidence)

    propagated = propagation_match(table, column, model, already_mapped)
    if propagated:
        add(*propagated)

    for element in catalog.elements:
        if not can_hold(column, element):
            continue
        add(element.name, alias_match(column.name, element))
        add(element.name, format_match(profile, element))
        add(element.name, check_digit_match(profile, element))
        if element.name in matches:
            add(element.name, name_token_match(column.name, element, table.name))

    # The name, the key or a check digit says what a column is; a shape or a
    # shared word only supports it.
    named = [m for m in matches.values() if m.kinds() & NAMING_KINDS]
    return sorted(named, key=lambda m: (-m.score(), m.element))
