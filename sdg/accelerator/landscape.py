"""Read a source's landscape: its structure, its keys, a profile of what matters.

The first thing the team needs at a new customer is the lay of the land: which
tables hold rows, which are referenced by many others, which are code lists,
which carry effective and end dates, which hold people. Discovery reads the
structure and tests for keys nobody declared; the profile then measures a
shortlist rather than every table, because a source of thousands of tables
holds a few hundred that anyone needs.

The shortlist is the largest and the most referenced tables, plus every table
they key to, so a level's table is profiled whenever a record table is. The
profile's pairwise measures (which columns decide others, children per
parent) run inside its time limit, and what it had to leave out is listed.

Nothing personal is kept. The profile withholds the values of every column the
base catalog marks sensitive (sdg/profiling/personal.py), and then, before
anything is summarized or saved, `scrub` keeps a value only where it is
known to be harmless (an allowlist, not a list of names to look for): a range
only for a date or number of few values named as a code, a kind, a flag or a
span's end; common values only for a short code of few values whose name says
nothing of a person, an account or a user, each held by many rows. The
model's constraints and defaults lose the values they quote.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from sdg.accelerator import sources
from sdg.catalog.loader import load_catalog
from sdg.catalog.model import Catalog
from sdg.discovery.classify import CODE_TABLE_MAX_ROWS, tokenize
from sdg.discovery.model import PhysicalModel
from sdg.discovery.run import discover
from sdg.mapping.scorer import map_columns
from sdg.profiling.model import ColumnProfile, ProfileReport
from sdg.profiling.personal import personal
from sdg.profiling.run import profile

BASE_CATALOG = Path(__file__).parent / "base_catalog.yaml"

SHORTLIST_SIZE = 60
HUBS_SHOWN = 10

# Name tokens that mark a date column as the start or the end of a span.
START_TOKENS = {"EFF", "EFFECTIVE", "START", "BEGIN"}
END_TOKENS = {"TERM", "END", "EXP", "EXPIRY", "EXPIRATION"}

# Catalog privacy classes that make a table hold people.
PERSONAL_CLASSES = {"sensitive", "identifier"}

# The scrub's allowlist. Common values are kept only for a column of few
# values (low or constant cardinality), each at most SHORT_VALUE characters,
# whose name holds none of DENY_TOKENS: a short code. A name, a login, an
# account or card number is never short and few at once often enough to trust.
SHORT_VALUE = 10
DENY_TOKENS = frozenset({"BY", "USER", "USERNAME", "OPERATOR", "EMP", "CUST", "CUSTOMER",
                         "CLIENT", "PAYEE", "ACCT", "ACCOUNT", "CARD", "IBAN", "NAME", "ADDR",
                         # Dates of a life, logins and staff, people named by
                         # role, contact details, record numbers.
                         "BRTH", "BDAY", "YOB", "DOD", "DEATH", "USR", "UID", "LOGIN", "OPER",
                         "NM", "MGR", "AGENT", "OFFICER", "OWNER", "CONTACT", "SSN", "PHONE",
                         "EMAIL", "ZIP", "MRN"})
# A date named as a birth date keeps no range, whatever the catalog says.
BIRTH_TOKENS = frozenset({"BIRTH", "DOB", "BIRTHDATE", "BDATE"})
FEW_VALUES = frozenset({"low", "constant"})
# A range (a date's or a number's least and greatest value) is kept only for a
# column whose name says it holds a code, a kind, a flag or a span's start or
# end: never by default.
RANGE_TOKENS = frozenset({"CD", "CODE", "TYPE", "TYP", "STS", "STATUS", "STAT", "IND", "FLG",
                          "FLAG", "YN", "EFF", "EFFECTIVE", "TERM", "END", "START", "BEGIN",
                          "EXP", "CAT", "CATEGORY", "CLASS", "CLS", "KIND", "LEVEL", "LVL",
                          "TIER", "GRADE"})
# A value kept is one held by at least this many rows, and a column keeps any
# value only when its rows are at least this many per value on average: a
# value few rows hold can point at the person in them.
MIN_VALUE_ROWS = 20
# The parts of a measure that are no value of the column: kept whatever else
# goes. A key outside these (a histogram's buckets, say) goes with the values.
CHECK_DIGIT_KEYS = frozenset({"scheme", "share", "tested"})
CHECK_DIGIT_SCHEMES = frozenset({"npi", "luhn"})
SHAPE_KEYS = frozenset({"kind", "skew", "is_sequence", "step", "agreement", "gaps",
                        "values_tested"})
RANGE_KEYS = frozenset({"min", "max", "mean", "stddev"})
# A quoted value, or a number, in a constraint's or a default's definition.
_LITERAL = re.compile(r"N?'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w\]\[.])-?\d+(?:\.\d+)?")
# A table this small holds too few rows for "few values" to mean a code:
# every column of a one-row table is constant, and its one row may be a person.
SCRUB_MIN_ROWS = CODE_TABLE_MAX_ROWS
# The placeholder dates a source writes for "none": the only placeholder
# values kept once a column's range is cleared.
SENTINEL_DATES = ("9999-", "1753-01-01", "1900-01-01")
# Characters a fitted format holds outside its classes, escapes and counts:
# when any are letters or digits, they are characters every value shares.
_FORMAT_SYNTAX = re.compile(r"\\.|\[[^\]]*\]|\{[^}]*\}")

logger = logging.getLogger(__name__)


@dataclass
class Landscape:
    name: str
    model: PhysicalModel
    profile: ProfileReport
    summary: dict[str, Any] = field(default_factory=dict)


def read(
    name: str,
    *,
    include_schemas: Iterable[str] | None = None,
    exclude_tables: Iterable[str] | None = None,
    shortlist_size: int = SHORTLIST_SIZE,
    max_minutes: float | None = None,
    registry: sources.SourceRegistry | None = None,
    allow_write_login: bool = False,
    on_stage: Callable[[str], None] | None = None,
) -> Landscape:
    """Discover the source, shortlist its tables, profile the shortlist.

    `max_minutes` limits the profile's pairwise measures (unset, the
    installation's SDG_PROFILE_MAX_MINUTES). The login is checked first and
    refused when it can write, unless `allow_write_login`; what was found is
    kept in the summary. `registry` is for a test; it must be a
    SourceRegistry, so every statement is still checked. `on_stage`, when
    given, is told each stage as it starts: "structure", then "profile"."""
    name = sources.plain_name(name)
    if registry is None:
        registry = sources.registry_for(name, allow_write_login=allow_write_login)
    elif not isinstance(registry, sources.SourceRegistry):
        raise TypeError("a source is read through a SourceRegistry only")
    started = time.perf_counter()
    if on_stage is not None:
        on_stage("structure")
    found = discover(registry, sources.SOURCE, include_schemas=include_schemas,
                     exclude_tables=exclude_tables, infer_keys=True)
    model = found.model
    chosen = shortlist(model, shortlist_size)
    catalog = load_catalog(BASE_CATALOG)
    if on_stage is not None:
        on_stage("profile")
    report = profile(registry, model, sources.SOURCE, tables=chosen,
                     max_minutes=max_minutes, catalog=catalog)
    scrub(model, report, catalog)
    landscape = Landscape(name=name, model=model, profile=report,
                          summary=summary(model, report, catalog, chosen))
    if registry.login is not None:
        landscape.summary["login"] = dict(registry.login)
    landscape.summary["seconds"] = round(time.perf_counter() - started, 1)
    logger.info("Read %s: %d tables, %d shortlisted, in %.0fs.", name,
                landscape.summary["tables"], len(chosen), landscape.summary["seconds"])
    return landscape


def _literal_format(held: ColumnProfile) -> bool:
    regex = (held.fitted_format or {}).get("regex") or ""
    return any(ch.isalnum() for ch in _FORMAT_SYNTAX.sub("", regex))


def _without_literals(definition: str | None, *, numbers: bool = True) -> str | None:
    """A definition with every quoted value (and, with `numbers`, every
    number) written as an empty one: the shape of a constraint or default is
    kept, the values it names are not."""
    if not definition:
        return definition
    plain = _LITERAL.sub("''", definition)
    return _NUMBER.sub("0", plain) if numbers else plain


def scrub_model(model: PhysicalModel) -> list[str]:
    """Clear the values a model quotes from its definitions: a check
    constraint's listed values and literals, a default's literal, a computed
    column's literals. Returns the columns and constraints that lost any."""
    cleared: list[str] = []
    for table in model.tables:
        for constraint in table.check_constraints:
            before = (constraint.definition, constraint.values)
            constraint.definition = _without_literals(constraint.definition) or ""
            constraint.values = None
            if (constraint.definition, constraint.values) != before:
                cleared.append(f"{table.name}.{constraint.name}")
        for column in table.columns:
            before = (column.check_definition, column.check_values, column.default_definition,
                      column.computed_definition)
            column.check_definition = _without_literals(column.check_definition)
            column.check_values = None
            column.default_definition = _without_literals(column.default_definition)
            # A computed column's numbers are its shape (a length, a style).
            column.computed_definition = _without_literals(column.computed_definition,
                                                           numbers=False)
            if (column.check_definition, column.check_values, column.default_definition,
                    column.computed_definition) != before:
                cleared.append(f"{table.name}.{column.name}")
    return cleared


def _kept(found: dict[str, Any] | None, keys: frozenset[str]) -> dict[str, Any] | None:
    if not found:
        return found
    held = {k: v for k, v in found.items() if k in keys}
    return held or None


def scrub(model: PhysicalModel, report: ProfileReport, catalog: Catalog | None = None) -> list[str]:
    """Keep a value in the landscape only where the allowlist says it is harmless.

    The model's definitions lose their literals (scrub_model). In the
    profile, a range (minimum, maximum, a distribution's or sequence's min
    and max) is kept only for a date or a number of few values whose name
    says it is a code, a kind, a flag or a span's end (RANGE_TOKENS), and
    nothing of a person (DENY_TOKENS, BIRTH_TOKENS); never for text, nor for
    a column of unique or many values. Common values are kept for a short
    code only (SHORT_VALUE, DENY_TOKENS), each only when MIN_VALUE_ROWS rows
    hold it. A column of fewer than MIN_VALUE_ROWS rows per value keeps no
    value at all. A measure that is one value goes with it, so does a fitted
    format whose literal characters every value shares, and so does any part
    of a measure not known to be a shape or a count (a histogram's buckets).
    Counts, lengths and shares stay. Returns what lost anything, as
    TABLE.COLUMN (or TABLE.CONSTRAINT)."""
    catalog = catalog or load_catalog(BASE_CATALOG)
    cleared: list[str] = scrub_model(model)
    for held in report.columns:
        table = model.table(held.table)
        column = table.column(held.column) if table is not None else None
        # Matched inside the name as well as whole: CREATEDBY and LASTNAME are
        # one token each. Matching too much only clears more.
        upper = held.column.upper()
        birth = any(t in upper for t in BIRTH_TOKENS) or personal(held, catalog) == "DOB"
        denied = any(t in upper for t in DENY_TOKENS)
        named = bool(set(tokenize(held.column)) & RANGE_TOKENS)
        few = held.cardinality in FEW_VALUES and held.row_count >= SCRUB_MIN_ROWS
        dense = bool(held.distinct_count) and (
            held.row_count / held.distinct_count >= MIN_VALUE_ROWS)
        keep_range = (column is not None and few and dense and named and not denied
                      and not birth and (column.is_date or column.is_numeric))
        length = held.max_length if held.max_length is not None else (
            column.character_length() if column is not None else None)
        short = column is not None and (not column.is_character or (
            length is not None and length <= SHORT_VALUE))
        keep_values = few and dense and short and not denied and not birth
        lost = False
        if not keep_range:
            lost = any(v is not None for v in (held.minimum, held.maximum, held.mean,
                                               held.stddev))
            # On a few rows a mean and spread give the values back.
            held.minimum = held.maximum = held.mean = held.stddev = None
            for measure in ("distribution", "sequence"):
                found = getattr(held, measure)
                if not found:
                    continue
                if found.get("kind") == "constant":
                    setattr(held, measure, None)
                    lost = True
                elif RANGE_KEYS & set(found):
                    setattr(held, measure, {k: v for k, v in found.items()
                                            if k not in RANGE_KEYS})
                    lost = True
            if held.placeholders:
                # A placeholder is the column's own extreme value; kept only
                # when it is a known "none" date, not an oldest birth date.
                sentinels = [ph for ph in held.placeholders
                             if str(ph.get("value") or "").startswith(SENTINEL_DATES)]
                lost = lost or len(sentinels) != len(held.placeholders)
                held.placeholders = sentinels or None
        # Whatever is left of a measure: its shape and counts only.
        allowed = SHAPE_KEYS | (RANGE_KEYS if keep_range else frozenset())
        for measure, keys in (("distribution", allowed), ("sequence", allowed),
                              ("check_digit", CHECK_DIGIT_KEYS)):
            found = getattr(held, measure)
            kept = _kept(found, keys)
            if kept != found:
                setattr(held, measure, kept)
                lost = True
        if held.check_digit and held.check_digit.get("scheme") not in CHECK_DIGIT_SCHEMES:
            held.check_digit = None
            lost = True
        if keep_values and held.top_values:
            common = [t for t in held.top_values if (t.count or 0) >= MIN_VALUE_ROWS]
            if len(common) != len(held.top_values):
                lost = True
                held.top_values = common or None
        if not keep_values:
            if held.top_values is not None or _literal_format(held):
                lost = True
            held.top_values = None
            if _literal_format(held):
                held.fitted_format = None
        if lost:
            cleared.append(held.qualified)
    return cleared


def in_degrees(model: PhysicalModel) -> dict[str, int]:
    """For each table, how many other tables key to it, declared or inferred."""
    children: dict[str, set[str]] = {}
    for table in model.tables:
        for key in table.foreign_keys:
            if key.parent_table != table.name:
                children.setdefault(key.parent_table, set()).add(table.name)
    return {parent: len(names) for parent, names in children.items()}


def shortlist(model: PhysicalModel, size: int = SHORTLIST_SIZE) -> list[str]:
    """The tables worth profiling: those holding rows, ranked by their row
    count and by how many tables key to them together, the first `size`; then
    every table one of those keys to, so each record's parents are read too."""
    filled = [t for t in model.tables if t.row_count > 0]
    degree = in_degrees(model)
    by_rows = {t.name: rank for rank, t in enumerate(
        sorted(filled, key=lambda t: (-t.row_count, t.name)))}
    by_degree = {t.name: rank for rank, t in enumerate(
        sorted(filled, key=lambda t: (-degree.get(t.name, 0), -t.row_count, t.name)))}
    ranked = sorted(filled, key=lambda t: (by_rows[t.name] + by_degree[t.name], t.name))
    chosen = [t.name for t in ranked[:max(0, size)]]
    held = set(chosen)
    for name in list(chosen):
        table = model.table(name)
        for key in table.foreign_keys if table else ():
            if key.parent_table not in held and model.table(key.parent_table) is not None:
                chosen.append(key.parent_table)
                held.add(key.parent_table)
    return chosen


def date_pairs(model: PhysicalModel, report: ProfileReport | None = None) -> dict[str, list[dict]]:
    """For each table, its date columns that read as a span's start and end,
    by their names. With a profile, how often the end holds a far-future
    placeholder: a span still open, which is what an "in force" filter reads."""
    found: dict[str, list[dict]] = {}
    for table in model.tables:
        dates = [c for c in table.columns if c.is_date]
        starts = [c for c in dates if set(tokenize(c.name)) & START_TOKENS]
        ends = [c for c in dates if set(tokenize(c.name)) & END_TOKENS]
        for start in starts:
            for end in ends:
                pair: dict[str, Any] = {"start": start.name, "end": end.name}
                held = report.column(table.name, end.name) if report else None
                if held is not None and held.placeholders:
                    pair["placeholders"] = sum(int(p.get("count") or 0) for p in held.placeholders)
                found.setdefault(table.name, []).append(pair)
    return found


def code_tables(model: PhysicalModel) -> list[str]:
    """Small tables other tables key to: lists of codes rather than a
    population, by the rule discovery classifies their keys with."""
    degree = in_degrees(model)
    return sorted(t.name for t in model.tables
                  if degree.get(t.name) and 0 < t.row_count < CODE_TABLE_MAX_ROWS)


def person_like(model: PhysicalModel, report: ProfileReport,
                catalog: Catalog) -> dict[str, list[str]]:
    """Tables with a column mapped outright to one of the catalog's personal elements
    (an accepted mapping), and those columns."""
    personal = {e.name for e in catalog.elements if e.pii in PERSONAL_CLASSES}
    found: dict[str, list[str]] = {}
    for mapping in map_columns(model, catalog, report).mappings:
        if mapping.accepted and mapping.element in personal:
            found.setdefault(mapping.table, []).append(mapping.column)
    return dict(sorted(found.items()))


def summary(model: PhysicalModel, report: ProfileReport, catalog: Catalog | None = None,
            chosen: list[str] | None = None) -> dict[str, Any]:
    """What a person reads first: counts, then the tables worth a look."""
    catalog = catalog or load_catalog(BASE_CATALOG)
    keys = [k for t in model.tables for k in t.foreign_keys]
    degree = in_degrees(model)
    hubs = sorted(degree, key=lambda name: (-degree[name], name))[:HUBS_SHOWN]
    rows = {t.name: t.row_count for t in model.tables}
    return {
        "tables": len(model.tables),
        "columns": sum(len(t.columns) for t in model.tables),
        "declared_keys": sum(1 for k in keys if k.kind == "declared"),
        "inferred_keys": sum(1 for k in keys if k.kind == "inferred"),
        "shortlisted": list(chosen) if chosen is not None else sorted(report.table_seconds),
        "skipped": [s.model_dump() for s in report.skipped],
        "hubs": [{"table": name, "referenced_by": degree[name], "rows": rows.get(name, 0)}
                 for name in hubs],
        "code_tables": code_tables(model),
        "date_pair_tables": date_pairs(model, report),
        "person_like_tables": person_like(model, report, catalog),
    }


MODEL_FILE = "model.json"
PROFILE_FILE = "profile.json"
SUMMARY_FILE = "landscape.json"


def save(landscape: Landscape) -> Path:
    """Write the model, the profile and the summary to the source's folder,
    the profile scrubbed (again: a landscape made elsewhere is held to it too)."""
    folder = sources.folder_for(landscape.name)
    scrub(landscape.model, landscape.profile)
    landscape.model.save(folder / MODEL_FILE)
    landscape.profile.save(folder / PROFILE_FILE)
    (folder / SUMMARY_FILE).write_text(
        json.dumps({"source": landscape.name, **landscape.summary}, indent=2, default=str) + "\n",
        encoding="utf-8")
    return folder


def load(name: str) -> Landscape:
    """The landscape last read for a source, from its folder."""
    folder = sources.folder_for(name)
    missing = [f for f in (MODEL_FILE, PROFILE_FILE, SUMMARY_FILE) if not (folder / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"{folder} has no {', '.join(missing)}: run `sdg accelerate landscape "
            f"{sources.plain_name(name)}` first")
    summary_held = json.loads((folder / SUMMARY_FILE).read_text(encoding="utf-8"))
    summary_held.pop("source", None)
    return Landscape(name=sources.plain_name(name),
                     model=PhysicalModel.load(folder / MODEL_FILE),
                     profile=ProfileReport.load(folder / PROFILE_FILE),
                     summary=summary_held)
