"""Build a business pack from an approved source plan.

`build` writes the pack folder under SDG_PACKS_DIR, outside the repository,
from the plan as it was approved -- refused unless it is approved and is
still that plan (approval.approved_plan) -- and the saved landscape:

    <id>/
      pack.json  features.json  story.json  glossary.json  column_names.json
      catalog.yaml  README.txt
      sql/store_tables.sql  sql/store_indexes.sql
      extract/<SOURCE>.sql

The folder is written first under <id>.building/, checked there -- its
pack.json by packs.check, its catalog by the catalog loader, and the whole
pack by starting SDM on it in a process of its own (startup.check) -- and
only then put in place, an earlier build kept as <id>.previous-<when>/ (never
removed by a build: old ones are the person's to remove by hand). The build's
hash is recorded in the store in the same transaction that finds the plan
still approved and unchanged, so a plan withdrawn or edited meanwhile is
never recorded as built.

The JSON files start from template/ (the banking pack with every business
value emptied), and the renderers below fill them through template/slots.json
only: a slot is a neutral name for a place in a pack file. A generated pack
offers pattern analysis only (modules ["pattern_analysis"], formats {}):
writing files from a pattern needs settings the plan cannot give.

Nothing here reads a source or a value of any source column: names come from
the model and the plan, words from the person.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from sdg import packs
from sdg.accelerator import approval, sources
from sdg.accelerator import landscape as reading
from sdg.accelerator import plan as planning
from sdg.accelerator import sqlgen, startup
from sdg.accelerator import structure as structure_rules
from sdg.accelerator.graph import KeyGraph
from sdg.accelerator.hierarchy import LevelCandidate
from sdg.accelerator.plan import SourcePlan
from sdg.accelerator.structure import DenormColumn, DenormTable
from sdg.accelerator.tables import ROLES

TEMPLATE = Path(__file__).parent / "template"
FILE_OF = {"pack": "pack.json", "features": "features.json", "story": "story.json",
           "glossary": "glossary.json"}
ROOT = Path(__file__).resolve().parents[2]
# What a build leaves in the pack folder besides the pack: who built it from
# which plan. A folder with it is one a later build may replace.
MARKER = packs.BUILD_MARKER
# The key, in the build's hash, of what the marker records for a load.
SEALED = "#load"
# What marks a staging folder as a build's own, so a later build may clear it.
STAGING_MARKER = ".sdm-staging"
BUILDING = ".building"
# An earlier build, moved aside: <id>.previous-<when built over>.
PREVIOUS = ".previous-"
# What report generation says of each field: a generated pack writes no files.
NOT_GENERATED = "No, not referred into and not generated"
LINE_WORDS = {"one": "line", "many": "lines"}
# Trailing name parts that say "a key" rather than what a level is.
_KEY_PARTS = frozenset({"ID", "CK", "CD", "CODE", "KEY", "NO", "NBR", "NUM", "SK"})
_UPPER_PARTS = frozenset({"ID", "CK", "CD", "SK"})
_INTEGER = frozenset({"bigint", "int", "smallint", "tinyint"})
_CHARACTER = frozenset({"char", "varchar", "nchar", "nvarchar"})
_ENTITY = re.compile(r"[a-z][a-z0-9_]{0,19}")
# One build at a time in a process: two would share a staging folder.
_BUILDING = threading.Lock()


class BuildRefused(ValueError):
    """A build the plan or the place refuses, in words a person reads."""


@dataclass
class BuildResult:
    folder: Path
    pack_id: str
    files: dict[str, str] = field(default_factory=dict)
    # Why the folder written was not put in place; empty when it was.
    problems: list[str] = field(default_factory=list)


# --- slots ---------------------------------------------------------------------

def _slots() -> dict[str, str]:
    return json.loads((TEMPLATE / "slots.json").read_text(encoding="utf-8"))["slots"]


class _Files:
    """The four JSON files, from the template, written through slots."""

    def __init__(self, entities: dict[str, str]) -> None:
        self.data = {part: json.loads((TEMPLATE / name).read_text(encoding="utf-8"))
                     for part, name in FILE_OF.items()}
        self.slots = _slots()
        self.entities = entities

    def _place(self, slot: str) -> tuple[str, list[str]]:
        if slot not in self.slots:
            raise KeyError(f"no slot {slot!r} in template/slots.json")
        place = self.slots[slot]
        for role, key in self.entities.items():
            place = place.replace(f"<{role}>", key)
        part, _, dotted = place.partition(":")
        return part, dotted.split(".")

    def put(self, slot: str, value: Any) -> None:
        part, steps = self._place(slot)
        node = self.data[part]
        for step in steps[:-1]:
            node = node.setdefault(step, {})
        node[steps[-1]] = value

    def get(self, slot: str) -> Any:
        part, steps = self._place(slot)
        node: Any = self.data[part]
        for step in steps:
            if not isinstance(node, dict) or step not in node:
                return None
            node = node[step]
        return node


# --- the plan, as the renderers read it ------------------------------------------

def humanize(name: str) -> str:
    """A column name as words: BRANCH_NAME -> "Branch name", X_ID -> "X ID"."""
    parts = [p for p in re.split(r"[_\s]+", name or "") if p]
    if not parts:
        return name
    words = [p.upper() if p.upper() in _UPPER_PARTS else p.lower() for p in parts]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def level_word(column: str) -> str:
    """What a level is called, from its key column: the key's own parts
    (ID, CODE...) dropped when something else is left."""
    parts = [p for p in re.split(r"[_\s]+", column or "") if p]
    while len(parts) > 1 and parts[-1].upper() in _KEY_PARTS:
        parts.pop()
    return humanize("_".join(parts) or column)


def _base_type(held: DenormColumn) -> str:
    return held.sql_type.split("(", 1)[0].strip().lower()


@dataclass
class _Level:
    level: LevelCandidate
    column: str
    label: str | None
    filter: str
    word: str
    ids: bool
    character: bool

    @property
    def walk(self) -> bool:
        return self.level.kind == "walk"


@dataclass
class _Feature:
    key: str
    column: str
    kind: str  # in | range
    label: str
    unit: str
    derive: dict[str, Any] | None = None


@dataclass
class _Role:
    role: str
    key: str
    denorm: DenormTable
    one: str
    many: str
    record_key: list[str]
    lines_key: list[str]
    lines: bool
    parent_key: list[str]
    levels: list[_Level]
    features: list[_Feature]

    @property
    def One(self) -> str:  # noqa: N802 -- the glossary's own form name
        return self.one[:1].upper() + self.one[1:]

    @property
    def Many(self) -> str:  # noqa: N802
        return self.many[:1].upper() + self.many[1:]

    @property
    def walks(self) -> list[_Level]:
        return [lv for lv in self.levels if lv.walk]

    @property
    def columns(self) -> list[str]:
        return [c.name for c in self.denorm.columns]

    @property
    def derived(self) -> list[str]:
        return [f.column for f in self.features if f.derive]


def entity_keys(plan: SourcePlan) -> dict[str, str]:
    """Each record kind's key in the pack: a plain slug of its many name (the
    role's own name while it has none), at most 20 characters, the two told
    apart when they would be the same."""
    out: dict[str, str] = {}
    for role in ROLES:
        kind = plan.record_kinds.get(role) or {}
        many = ((kind.get("name") or {}).get("many") or "").strip()
        key = structure_rules.slug(many)[:20].strip("_") or role
        if not _ENTITY.fullmatch(key):
            key = role
        if key in out.values():
            key = (key[:20 - len(role) - 1].strip("_") + "_" + role)
        out[role] = key
    return out


def _denorm(plan: SourcePlan, role: str) -> DenormTable:
    found = next((t for t in plan.structure if t.role == role), None)
    if found is None:
        raise BuildRefused(f"The {role} record kind has no denorm table: edit the plan once so "
                           f"its structure is proposed.")
    return found


def _named(denorm: DenormTable, table: str, column: str, what: str) -> DenormColumn:
    found = next((c for c in denorm.columns
                  if c.source_table == table and c.source_column == column), None)
    if found is None:
        raise BuildRefused(f"{what} ({table}.{column}) is not a column of {denorm.name}: edit "
                           f"the plan once so its structure is proposed again.")
    return found


def _roles(plan: SourcePlan) -> dict[str, _Role]:
    keys = entity_keys(plan)
    out: dict[str, _Role] = {}
    for role in ROLES:
        kind = plan.record_kinds.get(role)
        if not kind:
            raise BuildRefused(f"Choose the {role} record kind.")
        denorm = _denorm(plan, role)
        record, lines = kind["table"], kind.get("lines_table")
        name = kind.get("name") or {}
        record_key = [_named(denorm, record, c, f"the {role} record's key").name
                      for c in kind.get("key_columns") or []]
        if not record_key:
            raise BuildRefused(f"The {role} record kind has no key.")
        lines_key = [c.name for c in denorm.columns if c.serves == "lines_key"]
        parent_key = [c.name for c in denorm.columns
                      if c.serves == "key" and c.joins and role == "child"]
        levels = []
        for level in sorted((lv for lv in plan.levels(role) if lv.selected),
                            key=lambda lv: lv.order):
            key_column = _named(denorm, level.table, level.column, f"level {level.id}")
            label = None
            if level.label_table and level.label_column:
                named = _named(denorm, level.label_table, level.label_column,
                               f"level {level.id}'s label")
                # A withheld label loads empty: the level is shown by its key.
                label = named.name if named.privacy != "withheld" else None
            levels.append(_Level(
                level=level, column=key_column.name, label=label,
                filter=key_column.name.lower(), word=level_word(key_column.name),
                ids=_base_type(key_column) in _INTEGER and key_column.privacy != "surrogate",
                character=_base_type(key_column) in _CHARACTER))
        features = []
        taken = {lv.filter for lv in levels}
        for feature in plan.features(role):
            if not feature.selected:
                continue
            derive = feature.derive or None
            if derive:
                source = _named(denorm, record, derive["from"], f"feature {feature.id}'s input")
                column = feature.column
                key = column.strip("_").lower()
                held = _Feature(key=key, column=column, kind="range", label=humanize(key),
                                unit=str(derive.get("unit") or ""),
                                derive={"column": column, "op": derive.get("op", "elapsed"),
                                        "from": source.name, "unit": derive.get("unit")})
            else:
                named = _named(denorm, feature.table or record, feature.column,
                               f"feature {feature.id}")
                if named.privacy == "withheld":
                    continue  # loads empty: nothing to measure by
                column = named.name
                key = column.lower()
                held = _Feature(key=key, column=column,
                                kind="range" if feature.kind == "range" else "in",
                                label=humanize(column), unit="")
            if held.key in taken:
                continue
            taken.add(held.key)
            features.append(held)
        out[role] = _Role(role=role, key=keys[role], denorm=denorm,
                          one=(name.get("one") or role).strip(),
                          many=(name.get("many") or role).strip(),
                          record_key=record_key, lines_key=lines_key, lines=bool(lines),
                          parent_key=parent_key, levels=levels, features=features)
    if not out["child"].parent_key:
        raise BuildRefused("The child record kind does not join the primary.")
    return out


# --- the renderers ----------------------------------------------------------------

def _privacy_names(plan: SourcePlan) -> dict[str, list[str]]:
    """Each privacy class's column names across the denorm tables: one name
    holds one class in a pack's extract, or the runner could not tell them
    apart."""
    seen: dict[str, tuple[str, str]] = {}
    for denorm in plan.structure:
        for held in denorm.columns:
            other = seen.get(held.name.upper())
            if other is not None and other[0] != held.privacy:
                raise BuildRefused(
                    f"{held.name} is {other[0]} in {other[1]} and {held.privacy} in "
                    f"{denorm.name}: one name holds one privacy class in the pack's extract.")
            seen.setdefault(held.name.upper(), (held.privacy, denorm.name))
    out: dict[str, list[str]] = {p: [] for p in structure_rules.PRIVACY}
    for denorm in plan.structure:
        for held in denorm.columns:
            if held.name not in out[held.privacy]:
                out[held.privacy].append(held.name)
    return out


def _source_name(plan: SourcePlan) -> str:
    return sources.plain_name(plan.source)


def _render(plan: SourcePlan, graph: KeyGraph | None = None) -> _Files:
    """The four JSON files, filled from the plan."""
    roles = _roles(plan)
    files = _Files({r: roles[r].key for r in ROLES})
    _fill_pack(files, plan, roles, graph)
    _fill_features(files, plan, roles, graph)
    _fill_story(files, roles)
    _fill_glossary(files, roles)
    return files


def _fill_pack(files: _Files, plan: SourcePlan, roles: dict[str, _Role],
               graph: KeyGraph | None = None) -> None:
    pack_id = (plan.pack or {}).get("id") or "unnamed"
    primary, child = roles["primary"], roles["child"]
    files.put("pack_id", pack_id)
    files.put("pack_name", (plan.pack or {}).get("name") or pack_id)
    files.put("plan_name", f"{pack_id}-slice")
    child_entity: dict[str, Any] = {"role": "child", "parent": primary.key,
                                    "anchor": child.record_key[0],
                                    "key_columns": child.record_key + child.lines_key}
    if child.lines_key:
        child_entity["line_anchor"] = child.lines_key[0]
    files.put("record_kinds", {
        primary.key: {"role": "primary", "anchor": primary.record_key[0],
                      "key_column": primary.record_key[0]},
        child.key: child_entity})
    files.put("modules", ["pattern_analysis"])
    files.put("formats", {})
    source = _source_name(plan)
    names = _privacy_names(plan)
    extract: dict[str, Any] = {"default_source": source,
                               "kept": {source: names["kept"]},
                               "withheld": {source: names["withheld"]},
                               "surrogates": {source: names["surrogate"]}}
    told_apart = _case_sensitive_surrogates(plan, graph)
    if told_apart:
        extract["case_sensitive"] = {source: told_apart}
    files.put("extract", extract)


# A SQL Server collation that tells upper and lower case apart: _CS_ in its
# name, or a binary one (_BIN, _BIN2).
_TELLS_CASE = re.compile(r"_CS(_|$)|_BIN2?$", re.I)


def _case_sensitive_surrogates(plan: SourcePlan, graph: KeyGraph | None) -> list[str]:
    """The surrogate columns read from a source column whose collation tells
    case apart: their extract keeps each value's case when it makes the
    surrogate, so two keys differing only in case stay two records. One name
    is one surrogate in every table (the tables join on it), so a name read
    from columns that tell case apart and from columns that do not is
    refused: either way, one table's keys would no longer meet the other's."""
    if graph is None:
        return []
    told: dict[str, dict[bool, str]] = {}
    for denorm in plan.structure:
        for held in denorm.columns:
            if held.privacy != "surrogate":
                continue
            table = graph.table(held.source_table)
            column = table.column(held.source_column) if table is not None else None
            tells = bool(column is not None and column.collation
                         and _TELLS_CASE.search(column.collation))
            told.setdefault(held.name, {}).setdefault(
                tells, f"{held.source_table}.{held.source_column}")
    for name, ways in told.items():
        if len(ways) > 1:
            raise BuildRefused(
                f"{name} is read from {ways[True]}, which tells upper and lower case apart, "
                f"and from {ways[False]}, which does not, so one surrogate cannot stand for "
                f"both: give the two columns the same collation in the source, or keep "
                f"{name} in one table only.")
    return [name for name, ways in told.items() if True in ways]


def _filters(role: _Role) -> list[dict[str, Any]]:
    out = []
    for level in role.levels:
        held: dict[str, Any] = {"key": level.filter, "column": level.column,
                                "kind": "ids" if level.ids else "values"}
        if role.role == "child" and not level.ids:
            held["skip_blank"] = True
        out.append(held)
    return out


def _count(name: str) -> str:
    return "n_" + structure_rules.slug(name)


def _loader(role: _Role, columns_place: str, primary: _Role) -> dict[str, Any]:
    walks = role.walks
    record_count = _count(role.key)
    overview: list[list[str]] = []
    if role.lines:
        overview.append([_count(LINE_WORDS["many"]), "*"])
    overview.append([record_count, role.record_key[0]])
    if role.role == "child":
        overview.append([_count(primary.key), role.parent_key[0]])
    for level in walks:
        overview.append([_count(level.filter), level.column])
    sizes_levels, running = [], []
    for level in walks:
        running += [level.column] + ([level.label] if level.label else [])
        sizes_levels.append(list(running))
    scope, taken = [], []
    slugs: set[str] = set()
    for level in role.levels:
        # Two levels of one word (two "Group" levels) get one slug each.
        slug = structure_rules.slug(level.word) or level.filter
        named, number = slug, 2
        while named in slugs:
            named, number = f"{slug}_{number}", number + 1
        slugs.add(named)
        held: dict[str, Any] = {"level": named, "key": level.column}
        if level.label:
            held["label"] = level.label
        # Text is read trimmed: a label always, the key when it is text.
        held["trim"] = [c for c in (level.column, level.label)
                        if c and (c == level.label or level.character)]
        held["takes"] = list(taken)
        held["order"] = level.label or level.column
        scope.append(held)
        if level.walk:
            taken.append(level.filter)
    character = {c.name for c in role.denorm.columns if _base_type(c) in _CHARACTER}
    keys = set(role.record_key + role.lines_key + role.parent_key)
    loader: dict[str, Any] = {
        "table": role.denorm.name,
        "columns": columns_place,
        "order": role.record_key + role.lines_key,
        "filters": _filters(role),
        "derive": [f.derive for f in role.features if f.derive],
        "labels": role.record_key[:1] + ([walks[0].column] if walks else []),
        "strip": [c for c in role.columns if c in character and c not in keys],
        "empty": "No " + role.many.replace("{", "{{").replace("}", "}}")
                 + " match the applied filters: {filters}",
        "overview": overview,
        "sizes": {"levels": sizes_levels,
                  "count": _count(LINE_WORDS["many"]) if role.lines else record_count,
                  "trim": any(lv.character for lv in walks)},
        "scope": scope,
        "counts": ({"records": record_count, "lines": _count(LINE_WORDS["many"])}
                   if role.lines else {"records": record_count}),
    }
    return loader


def _defined(role: _Role) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for level in role.levels:
        held: dict[str, Any] = {"key": level.filter, "label": level.word, "kind": "in"}
        if level.walk:
            held["scope"] = True
        held["column"] = level.column
        if level.label:
            held["describe"] = level.label
        out.append(held)
    for feature in role.features:
        held = {"key": feature.key, "label": feature.label, "kind": feature.kind,
                "column": feature.column}
        if feature.kind == "range":
            held["unit"] = feature.unit
        out.append(held)
    return out


def _report_fields(role: _Role) -> list[dict[str, Any]]:
    basis = {"basis": "child"} if role.role == "child" else {}
    out: list[dict[str, Any]] = []
    for feature in role.features:
        if feature.derive and feature.unit == "years":
            out.append({"key": f"{feature.key}_band", "label": f"{feature.label} band",
                        "column": feature.column, "bands": "age", **basis})
        elif feature.kind == "in":
            out.append({"key": feature.key, "label": feature.label, "column": feature.column,
                        **basis})
    for level in role.levels:
        held = {"key": level.filter, "label": level.word, "column": level.column}
        if level.label:
            held["describe"] = level.label
        out.append({**held, **basis})
    return out


def _dimensions(role: _Role) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"key": f.key, "label": f.label, "column": f.column}
                                 for f in role.features if f.kind == "in"]
    out += [{"key": f"{f.key}_band", "label": f"{f.label} band", "column": f.column,
             "bands": True} for f in role.features if f.derive and f.unit == "years"]
    return out


def _type_level(child: _Role) -> _Level | None:
    """The child's first selected narrowing level: what sorts its records
    into kinds."""
    return next((lv for lv in child.levels if not lv.walk), None)


def _geography(plan: SourcePlan, primary: _Role, graph: KeyGraph | None) -> dict[str, str]:
    if graph is None:
        return {}
    out = {}
    for held in primary.denorm.columns:
        element = graph.element_of(held.source_table, held.source_column)
        if element in structure_rules.PLACE_ELEMENTS and held.privacy == "kept":
            out[held.name] = element
    return out


def _fill_features(files: _Files, plan: SourcePlan, roles: dict[str, _Role],
                   graph: KeyGraph | None) -> None:
    primary, child = roles["primary"], roles["child"]
    both = (primary, child)
    columns_place = {r.role: files.slots[f"{r.role}_columns"].partition(":")[2] for r in both}
    files.put("continuous_features", [f.column for f in primary.features if f.kind == "range"])
    files.put("categorical_features", [f.column for f in primary.features if f.kind == "in"])
    files.put("geography_columns", _geography(plan, primary, graph))
    files.put("description_columns", list(dict.fromkeys(
        lv.label for r in both for lv in r.levels if lv.label)))
    filter_columns: dict[str, str] = {}
    for role in both:
        for level in role.levels:
            other = filter_columns.setdefault(level.filter, level.column)
            if other != level.column:
                raise BuildRefused(
                    f"The filter {level.filter} would read {other} for one record kind and "
                    f"{level.column} for the other: one filter reads one column. Deselect one "
                    f"of the two levels.")
    files.put("filter_columns", filter_columns)
    labels: dict[str, list[Any]] = {}
    for role in both:
        for feature in role.features:
            unit = feature.unit or None
            labels.setdefault(feature.column, [feature.label, unit])
            labels.setdefault(feature.key, [feature.label, unit])
    files.put("feature_labels", labels)

    files.put("primary_columns", primary.columns)
    files.put("child_columns", child.columns)
    files.put("primary_key", primary.record_key[0])
    files.put("report_keys", {primary.key: primary.record_key,
                              child.key: child.record_key + child.lines_key})
    names = _privacy_names(plan)
    personal = names["withheld"] + names["surrogate"]
    files.put("personal_columns", personal)
    files.put("personal_columns_rows", list(personal))
    files.put("child_filters", [lv.filter for lv in child.levels])
    combo = list(dict.fromkeys(lv.filter for r in both for lv in r.walks))
    files.put("combo_key", combo)
    kind_level = _type_level(child)
    files.put("type_filter", kind_level.filter if kind_level else None)
    first = (primary.walks or child.walks)[0] if (primary.walks or child.walks) else None
    files.put("baseline_scope", {"prefix": first.column if first else "",
                                 "label": first.word.lower() if first else ""})

    files.put("defined_patterns", {r.key: _defined(r) for r in both})
    files.put("discovery_defaults", {r.key: [f.key for f in r.features] for r in both})
    files.put("scope_levels", list(combo))
    dimensions = {r.key: _dimensions(r) for r in both}
    files.put("scenario_dimensions", dimensions)
    files.put("scenario_split", {k: [d["key"] for d in v[:2]] for k, v in dimensions.items()})
    files.put("primary_report_fields", _report_fields(primary))
    files.put("child_report_fields", _report_fields(child))
    files.put("primary_over_child", [])
    files.put("report_generation", {
        r.key: {f["key"]: NOT_GENERATED for f in _report_fields(r)} for r in both})

    files.put("loaders", {r.key: _loader(r, columns_place[r.role], primary) for r in both})

    files.put("child_record_key", child.record_key[0])
    files.put("child_primary_key", child.parent_key[0])
    files.put("child_type_column", kind_level.column if kind_level else None)
    ranges = [f for f in child.features if f.kind == "range"]
    kinds = [f for f in child.features if f.kind == "in"]
    files.put("child_histograms", {f.key: f.column for f in ranges})
    files.put("child_continuous", [[f.column, f.key] for f in ranges])
    categorical = [[f.column, f.key] for f in kinds]
    if kind_level and kind_level.filter not in {f.key for f in kinds}:
        categorical.append([kind_level.column, kind_level.filter])
    files.put("child_categorical", categorical)
    files.put("child_shown_continuous", [f.key for f in ranges])
    files.put("child_shown_categorical", [k for _, k in categorical])
    files.put("child_name_type", kind_level.filter if kind_level else None)
    amount = next((f for f in ranges if not f.derive), None)
    files.put("child_name_amount", amount.key if amount else None)
    files.put("child_name_amount_words", f"avg {amount.label.lower()}" if amount else None)
    files.put("child_name_fallback", f"{child.One} pattern")
    lines_table = (plan.record_kinds["child"] or {}).get("lines_table")
    files.put("child_line_prefixes", [c.name for c in child.denorm.columns
                                      if lines_table and c.source_table == lines_table])

    files.put("primary_histograms", {f.column: f.column for f in primary.features
                                     if f.kind == "range"})
    files.put("primary_naming_levels", {lv.filter: {"fallback": "stripped", "length": 16}
                                        for lv in primary.walks})
    age = next((f for f in primary.features if f.derive and f.unit == "years"), None)
    files.put("primary_age_feature", age.column if age else None)
    facts: dict[str, Any] = {}
    if age is not None:
        facts["age"] = [age.column, None]
    tenure = next((f for f in primary.features if f.derive and f.unit == "months"), None)
    if tenure is not None:
        facts["tenure"] = tenure.column
    if kind_level is not None:
        facts["type"] = kind_level.filter
    files.put("screen_facts", facts)


def _words(*names: str) -> str:
    """Words as a regular expression's alternatives, each matched whole."""
    held = sorted({re.escape(n.lower()) for n in names if n}, key=lambda w: (-len(w), w))
    return "|".join(held)


def _fill_story(files: _Files, roles: dict[str, _Role]) -> None:
    primary, child = roles["primary"], roles["child"]
    stop = list(files.get("stop_words") or [])
    for role in (primary, child):
        for word in role.many.lower().split():
            if word not in stop:
                stop.append(word)
    files.put("stop_words", stop)
    child_words = [child.one, child.many] + ([LINE_WORDS["one"], LINE_WORDS["many"]]
                                             if child.lines else [])
    files.put("reading_child_words", rf"\b({_words(*child_words)})\b")
    files.put("reading_names", list(dict.fromkeys(
        [lv.filter for r in (primary, child) for lv in r.levels]
        + [f.key for r in (primary, child) for f in r.features if f.kind == "in"])))
    age = next((f for f in primary.features if f.derive and f.unit == "years"), None)
    files.put("reading_ages", {"primary": age.key if age else None, "child": None})
    amount = next((f for f in child.features if f.kind == "range" and not f.derive), None)
    files.put("reading_amount", amount.key if amount else None)
    files.put("reading_count_nouns", _words(primary.one, primary.many, child.one, child.many,
                                            "records", "record"))
    files.put("reading_data_types", [
        [child.key, rf"\b({_words(*child_words)})\b"],
        [primary.key, rf"\b({_words(primary.one, primary.many)})\b"]])


def _fill_glossary(files: _Files, roles: dict[str, _Role]) -> None:
    primary, child = roles["primary"], roles["child"]
    for role, slot in ((primary, "primary_glossary"), (child, "child_glossary")):
        words: dict[str, Any] = {"one": role.one, "many": role.many, "One": role.One,
                                 "Many": role.Many}
        counts: list[list[Any]] = [[_count(role.key), None]]
        if role.lines:
            words.update({"line": LINE_WORDS["one"], "lines": LINE_WORDS["many"],
                          "Line": LINE_WORDS["one"].capitalize(),
                          "Lines": LINE_WORDS["many"].capitalize()})
            counts.append([_count(LINE_WORDS["many"]), LINE_WORDS["many"].capitalize()])
        counts += [[_count(lv.filter), lv.word] for lv in role.walks]
        words["counts"] = counts
        if role.role == "child":
            words["types"] = {}
        walks = [lv.word for lv in role.walks]
        words["depths"] = [" and ".join(walks[:i]) for i in range(1, len(walks) + 1)]
        files.put(slot, words)
    ranges = {f.column: {"label": f.label, "unit": f.unit}
              for r in (primary, child) for f in r.features if f.kind == "range"}
    mixes = {f.column: f.label for r in (primary, child) for f in r.features if f.kind == "in"}
    files.put("chart_ranges", ranges)
    files.put("chart_mixes", mixes)
    level_words = list(dict.fromkeys(lv.word.lower() for r in (primary, child)
                                     for lv in r.levels))
    examples = (", ".join(level_words[:-1]) + " or " + level_words[-1]
                if len(level_words) > 1 else (level_words[0] if level_words else "a level"))
    phrases = dict(files.get("phrases") or {})
    phrases.update({
        "field_examples": f"a {examples}",
        "outside_range_risk": f"the pattern's {child.many} may not suit them.",
        "scope_heading": " and ".join(lv.word for lv in primary.walks) or primary.Many,
        "joined_heading": "Linked records",
        "joined_words": ""})
    files.put("phrases", phrases)


def pack_json(plan: SourcePlan) -> dict[str, Any]:
    """pack.json: the record kinds, pattern analysis only, the extract's
    privacy classes under the source's name, and the store scripts."""
    return _render(plan).data["pack"]


def features_json(plan: SourcePlan, graph: KeyGraph | None = None) -> dict[str, Any]:
    """features.json: loaders, store, patterns, the child's and the
    primary's analysis, all from the levels and features. With the key graph,
    the primary's places are named too (analyzer geography)."""
    return _render(plan, graph).data["features"]


def story_json(plan: SourcePlan) -> dict[str, Any]:
    return _render(plan).data["story"]


def glossary_json(plan: SourcePlan) -> dict[str, Any]:
    """glossary.json: the person's names for each record kind."""
    return _render(plan).data["glossary"]


def column_names_json(plan: SourcePlan) -> dict[str, str]:
    """column_names.json: a business name for every denorm column: a level's
    key and label by the level's word, the rest by their names."""
    roles = _roles(plan)
    out: dict[str, str] = {}
    for role in roles.values():
        words = {}
        for level in role.levels:
            words[level.column] = humanize(level.column)
            if level.label:
                words[level.label] = level.word
        for held in role.denorm.columns:
            out.setdefault(held.name, words.get(held.name) or humanize(held.name))
        for feature in role.features:
            out.setdefault(feature.column, feature.label)
    return out


def catalog_yaml(plan: SourcePlan, graph: KeyGraph) -> str:
    """catalog.yaml: the base catalog, each element's aliases extended by the
    denorm columns the base catalog maps to it."""
    base = yaml.safe_load(reading.BASE_CATALOG.read_text(encoding="utf-8"))
    mapped = graph.mapped()
    extra: dict[str, list[str]] = {}
    for denorm in plan.structure:
        for held in denorm.columns:
            element = mapped.get((held.source_table, held.source_column))
            if element:
                extra.setdefault(element, [])
                if held.name not in extra[element]:
                    extra[element].append(held.name)
    for element in base.get("elements") or []:
        aliases = list(element.get("aliases") or [])
        for name in extra.get(element.get("name"), []):
            if name not in aliases:
                aliases.append(name)
        element["aliases"] = aliases
    base["organization"] = (plan.pack or {}).get("name") or base.get("organization")
    header = (f"# The {(plan.pack or {}).get('id')} pack's element catalog: the accelerator's "
              f"base catalog,\n# with the source's denorm columns as aliases. Written from "
              f"{plan.source}'s approved plan.\n\n")
    return header + yaml.safe_dump(base, sort_keys=False, allow_unicode=True, width=100)


# --- the dry run --------------------------------------------------------------------

def _config_columns(files: _Files, roles: dict[str, _Role]) -> dict[str, list[tuple[str, str]]]:
    """Every column the generated configuration names, by role ("both" for
    a place that names either's), each with where it is named."""
    out: dict[str, list[tuple[str, str]]] = {"primary": [], "child": [], "both": []}

    def add(role: str, where: str, *names: Any) -> None:
        for name in names:
            if isinstance(name, str) and name and name != "*":
                out[role].append((where, name))

    loaders = files.get("loaders") or {}
    for role in roles.values():
        loader = loaders.get(role.key) or {}
        where = f"the {role.role} loader"
        add(role.role, where, *loader.get("order", []), *loader.get("labels", []),
            *loader.get("strip", []))
        for held in loader.get("filters", []):
            add(role.role, where, held.get("column"))
        for held in loader.get("derive", []):
            add(role.role, where, held.get("from"))
        for _, of in loader.get("overview", []):
            add(role.role, where, of)
        for level in (loader.get("sizes") or {}).get("levels", []):
            add(role.role, where, *level)
        for held in loader.get("scope", []):
            add(role.role, where, held.get("key"), held.get("label"), held.get("order"),
                *held.get("trim", []))
        for slot in ("defined_patterns", "scenario_dimensions"):
            for held in (files.get(slot) or {}).get(role.key, []):
                add(role.role, slot.replace("_", " "), held.get("column"), held.get("describe"))
        for held in files.get(f"{role.role}_report_fields") or []:
            add(role.role, "report fields", held.get("column"), held.get("describe"))
        add(role.role, "report keys", *(files.get("report_keys") or {}).get(role.key, []))
        add(role.role, "the columns read", *(files.get(f"{role.role}_columns") or []))
    add("primary", "the primary key", files.get("primary_key"))
    add("primary", "the primary's analysis", *(files.get("primary_histograms") or {}).values())
    child_where = "the child's analysis"
    add("child", child_where, files.get("child_record_key"), files.get("child_primary_key"),
        files.get("child_type_column"), *(files.get("child_histograms") or {}).values(),
        *(files.get("child_line_prefixes") or []))
    for slot in ("child_continuous", "child_categorical"):
        add("child", child_where, *[pair[0] for pair in files.get(slot) or []])
    add("both", "the analyzer's columns", *(files.get("continuous_features") or []),
        *(files.get("categorical_features") or []), *(files.get("description_columns") or []),
        *(files.get("filter_columns") or {}).values(),
        *(files.get("geography_columns") or {}).keys())
    add("both", "the personal columns", *(files.get("personal_columns") or []),
        *(files.get("personal_columns_rows") or []))
    return out


def shape_problems(plan: SourcePlan) -> list[str]:
    """What a saved plan holds that no edit makes: a role other than primary
    and child, a grain other than record or lines, two tables for a role."""
    found = [f"The plan names a record kind {k!r}: only primary and child are built."
             for k in plan.record_kinds if k not in ROLES]
    for denorm in plan.structure:
        if denorm.role not in ROLES or denorm.grain not in structure_rules.GRAINS:
            found.append(f"A denorm table has role {denorm.role!r} and grain "
                         f"{denorm.grain!r}: neither is one a build writes.")
    roles = [t.role for t in plan.structure]
    found += [f"The {r} record kind has {roles.count(r)} denorm tables: one is built."
              for r in ROLES if roles.count(r) > 1]
    return found


def privacy_problems(plan: SourcePlan, graph: KeyGraph | None = None) -> list[str]:
    """Columns whose privacy class no edit could have given them: a locked
    column kept and, given the key graph, any class the column's own
    proposal (structure.privacy_for, read again from the model) refuses --
    so a plan file edited by hand cannot keep what identifies a person."""
    found = []
    # The structure proposed afresh from the plan's levels, features and keys:
    # what each column serves, whether the tables join on it, its proposal
    # (both sides of a join made one) and its lock come from here, never from
    # the plan file, which a hand edit could change.
    proposed: dict[tuple[str, str, str], DenormColumn] = {}
    if graph is not None:
        for table in structure_rules.propose(plan, graph):
            for column in table.columns:
                proposed[(table.role, column.source_table, column.source_column)] = column
    for denorm in plan.structure:
        for held in denorm.columns:
            where = f"{denorm.name}.{held.name}"
            if held.privacy == "kept" and held.locked:
                found.append(f"{where} is locked and cannot be kept: {held.reason}")
                continue
            if graph is None:
                continue
            fresh = proposed.get((denorm.role, held.source_table, held.source_column))
            if fresh is None and held.serves != "extra":
                found.append(f"{where} is not a column the plan's keys, levels or features "
                             f"give the {denorm.role} table: edit the plan once so its "
                             f"structure is proposed again.")
                continue
            if fresh is not None and (fresh.serves, fresh.joins) != (held.serves, held.joins):
                found.append(f"{where} is saved as serving {held.serves}, but it serves "
                             f"{fresh.serves}: edit the plan once so its structure is proposed "
                             f"again.")
                continue
            if fresh is None:  # a column the person added
                privacy, reason, locked = structure_rules.privacy_for(
                    graph, held.source_table, held.source_column, "extra")
            else:
                privacy, reason, locked = fresh.proposed, fresh.reason, fresh.locked
            probe = structure_rules.DenormColumn(**{**held.to_dict(), "locked": locked,
                                                    "proposed": privacy, "reason": reason})
            refused = structure_rules.refusal(probe, held.privacy, graph)
            if refused:
                found.append(f"{where}: {refused}")
            elif locked and (held.privacy != privacy or not held.locked):
                found.append(f"{where} is {held.privacy}, but its proposal is {privacy} and "
                             f"locked: {reason}")
            elif held.privacy == "kept" and privacy == "surrogate" \
                    and not held.confirmed_not_personal:
                # The other side of a join it shares with a key that may say
                # who a row is about.
                found.append(f"{where} is kept, but its proposal is a surrogate: {reason}. "
                             f"Keep it only by confirming it holds nothing personal.")
    return found


def config_problems(plan: SourcePlan, graph: KeyGraph | None = None) -> list[str]:
    """The dry run approval gates submit on: render the configuration from
    the plan, and name every column it names that is not in the structure;
    also a plan's shape and privacy classes no edit would make. Empty when
    there is nothing to say -- including a plan without both record kinds or
    a structure, which the other gates name."""
    shape = shape_problems(plan)
    if shape:
        return shape
    privacy = privacy_problems(plan, graph)
    if privacy:
        return privacy
    if not all(plan.record_kinds.get(r) for r in ROLES) or \
            not all(any(t.role == r for t in plan.structure) for r in ROLES):
        return []
    try:
        roles = _roles(plan)
        files = _render(plan, graph)
    except (BuildRefused, packs.PackError, sources.SourceError) as exc:
        return [str(exc)]
    allowed = {r: set(roles[r].columns) | set(roles[r].derived) for r in ROLES}
    allowed["both"] = allowed["primary"] | allowed["child"]
    found = []
    for role, named in _config_columns(files, roles).items():
        for where, name in named:
            if name not in allowed[role]:
                problem = (f"The generated configuration names {name} ({where}), which is not "
                           f"a column of the {'denorm tables' if role == 'both' else role} "
                           f"structure.")
                if problem not in found:
                    found.append(problem)
    if graph is not None and not found:
        found += sql_problems(plan, graph)
    return found


def sql_problems(plan: SourcePlan, graph: KeyGraph) -> list[str]:
    """The store tables, their indexes and the extract, written from the
    plan as a build would: what stops any of them, in words. Empty when all
    three can be written."""
    try:
        sqlgen.store_tables_sql(plan, graph, [])
        sqlgen.store_indexes_sql(plan, [])
        sqlgen.extract_sql(plan, graph, approved_by="", approved_at="",
                           plan_hash=planning.plan_hash(plan))
    except (ValueError, KeyError, StopIteration, packs.PackError) as exc:
        return [f"The pack's SQL cannot be written: {exc}"]
    return []


# --- writing ---------------------------------------------------------------------------

def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def built_files(source: str) -> list[str]:
    """The files a build writes, by their path in the pack folder."""
    return ["pack.json", "features.json", "story.json", "glossary.json", "column_names.json",
            "catalog.yaml", "README.txt", "sql/store_tables.sql", "sql/store_indexes.sql",
            f"extract/{sources.plain_name(source)}.sql"]


def render(plan: SourcePlan, graph: KeyGraph, *, approved_by: str, approved_at: Any,
           plan_hash: str, databases: list[str], packs_dir: str) -> dict[str, str]:
    """Every file of the pack, by its path in the folder."""
    files = _render(plan, graph)
    source = _source_name(plan)
    pack_id = files.get("pack_id")
    try:
        texts = {
            "pack.json": _dump(files.data["pack"]),
            "features.json": _dump(files.data["features"]),
            "story.json": _dump(files.data["story"]),
            "glossary.json": _dump(files.data["glossary"]),
            "column_names.json": _dump(column_names_json(plan)),
            "catalog.yaml": catalog_yaml(plan, graph),
            "README.txt": sqlgen.readme_txt(plan, folder_name=pack_id, packs_dir=packs_dir),
            "sql/store_tables.sql": sqlgen.store_tables_sql(plan, graph, databases),
            "sql/store_indexes.sql": sqlgen.store_indexes_sql(plan, databases),
            f"extract/{source}.sql": sqlgen.extract_sql(plan, graph, approved_by=approved_by,
                                                        approved_at=approved_at,
                                                        plan_hash=plan_hash),
        }
    except sqlgen.SqlRefused as exc:
        raise BuildRefused(str(exc)) from exc
    if sorted(texts) != sorted(built_files(source)):
        raise BuildRefused("the files rendered are not the files a build writes")
    return texts


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def built_hash(files: dict[str, str], sealed: dict[str, Any] | None = None) -> str:
    """One hash over every file's, and over what the marker records for a
    load (`sealed`: the source columns read and the store layout): what the
    store records as built."""
    held = dict(files)
    if sealed is not None:
        held[SEALED] = _sha(json.dumps(sealed, sort_keys=True))
    return _sha(json.dumps(held, sort_keys=True))


def sealed(reads: Any, store: Any) -> dict[str, Any]:
    """What a load reads from the marker, as the build's hash covers it."""
    return {"reads": reads, "store": store}


def _inside(path: Path, folder: Path) -> bool:
    a, b = os.path.normcase(str(path)), os.path.normcase(str(folder))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # different drives
        return False


def packs_folder(packs_dir: str | Path | None = None) -> Path:
    """Where generated packs go: SDG_PACKS_DIR (or the folder given), never
    inside the repository."""
    from sdg.config import load_env

    load_env()
    raw = str(packs_dir) if packs_dir is not None else os.getenv("SDG_PACKS_DIR", "").strip()
    if not raw.strip():
        raise BuildRefused("Set SDG_PACKS_DIR in .env to a folder outside SDM, where generated "
                           "packs are kept, then build again.")
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise BuildRefused(f"SDG_PACKS_DIR={raw!r} is not a full path: name the folder from its "
                           f"drive.")
    resolved = folder.resolve()
    if _inside(resolved, ROOT):
        raise BuildRefused(f"{resolved} is inside SDM's own folder: a generated pack is kept "
                           f"outside it. Set SDG_PACKS_DIR to a folder elsewhere.")
    if resolved.exists() and not resolved.is_dir():
        raise BuildRefused(f"{resolved} is there but is not a folder.")
    return resolved


def check_pack_id(pack_id: Any) -> str:
    """A pack id a build may write: plain, and neither a shipped pack nor any
    folder of SDM's own packs."""
    if not isinstance(pack_id, str) or not planning.PACK_ID.fullmatch(pack_id):
        raise BuildRefused("Name the pack: its id is 2 to 32 lowercase letters, digits or _, "
                           "starting with a letter.")
    if pack_id in planning.SHIPPED_PACKS or (packs.BUILT_IN / pack_id).exists():
        raise BuildRefused(f"{pack_id} is a pack shipped with SDM: a build never writes it. "
                           f"Name the pack another id.")
    return pack_id


def _linked(path: Path) -> bool:
    """A link or a junction: somewhere else, never cleared or replaced."""
    return path.is_symlink() or path.is_junction()


def _fresh_staging(staging: Path) -> None:
    """The staging folder, empty: one a build left is cleared; anything else
    there is refused, never removed."""
    if staging.exists() or _linked(staging):
        if _linked(staging) or not (staging / STAGING_MARKER).is_file():
            raise BuildRefused(f"{staging} is there and is not a build's own: move it aside, "
                               f"then build again.")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / STAGING_MARKER).write_text("A build's staging folder; a later build clears it.\n",
                                          encoding="utf-8")


def _write(folder: Path, texts: dict[str, str]) -> dict[str, str]:
    hashes = {}
    for name, text in texts.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        hashes[name] = _sha(text)
    return hashes


def previous_folder(target_dir: Path, pack_id: str, when: datetime | None = None) -> Path:
    """Where an earlier build is moved aside: <id>.previous-<when>, a name no
    folder has yet (a number after it when one built in the same second
    has), so no earlier build is ever replaced."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = target_dir / f"{pack_id}{PREVIOUS}{stamp}"
    number = 2
    while folder.exists() or _linked(folder):
        folder = target_dir / f"{pack_id}{PREVIOUS}{stamp}-{number}"
        number += 1
    return folder


@dataclass
class _Swap:
    staged: Path
    target: Path
    previous: Path
    moved_previous: bool = False
    installed: bool = False

    def install(self) -> None:
        """The staged folder in place, an earlier build moved aside to
        .previous-<when>; no earlier build is ever removed."""
        if _linked(self.target) or _linked(self.previous):
            raise BuildRefused(f"{self.target} or {self.previous} is a link to somewhere else: "
                               f"move it aside, then build again.")
        if self.target.exists():
            if self.previous.exists():
                raise BuildRefused(f"{self.previous} is there already: build again.")
            os.replace(self.target, self.previous)
            self.moved_previous = True
        try:
            os.replace(self.staged, self.target)
        except OSError:
            if self.moved_previous:
                os.replace(self.previous, self.target)
                self.moved_previous = False
            raise
        self.installed = True

    def undo(self) -> None:
        """Back as it was: the staged folder out, the earlier build back."""
        if self.installed:
            os.replace(self.target, self.staged)
            self.installed = False
        if self.moved_previous:
            os.replace(self.previous, self.target)
            self.moved_previous = False


def _protected(source: str) -> list[str]:
    """The databases the store scripts refuse: the source's, and every
    protected one."""
    from sdg.config import protected_databases

    database = sources._setting(source, "DATABASE", source)
    return sorted({database, *protected_databases()})


def build(source: str, *, by: str, packs_dir: str | Path | None = None) -> BuildResult:
    """Build the pack of `source`'s approved plan. Refused, with nothing
    written, unless the plan is approved and unchanged, its pack id is one a
    build may write, and SDG_PACKS_DIR (or `packs_dir`) is a folder outside
    SDM. A pack that does not start is left in <id>.building/ and its
    problems returned; one that does is put in place and recorded as built."""
    if not _BUILDING.acquire(blocking=False):
        raise BuildRefused("A pack is being built already: wait for it to finish.")
    try:
        return _build(source, by=by, packs_dir=packs_dir)
    finally:
        _BUILDING.release()


def _build(source: str, *, by: str, packs_dir: str | Path | None) -> BuildResult:
    source = sources.plain_name(source)
    target_dir = packs_folder(packs_dir)
    held = approval.approved_plan(source)
    digest = planning.plan_hash(held)
    pack_id = check_pack_id((held.pack or {}).get("id"))
    row = approval.record(source) or {}
    read = reading.load(source)
    planning.refuse_stale(held, read)
    graph = planning.graph_for(read, held)
    found = config_problems(held, graph)
    if found:
        raise BuildRefused("The plan cannot be built: " + " ".join(found))
    texts = render(held, graph, approved_by=str(row.get("approved_by") or ""),
                   approved_at=row.get("approved_at") or "", plan_hash=digest,
                   databases=_protected(source), packs_dir=str(target_dir))
    packs.check(json.loads(texts["pack.json"]), f"{pack_id}/pack.json")
    if json.loads(texts["pack.json"])["id"] != pack_id:
        raise BuildRefused("the pack written does not carry the plan's pack id")

    previous = previous_folder(target_dir, pack_id)
    for path in (target_dir / pack_id, target_dir / f"{pack_id}{BUILDING}", previous):
        if _inside(ROOT, path.resolve()) or _inside(path.resolve(), ROOT):
            raise BuildRefused(f"{path} is, or holds, SDM's own folder: set SDG_PACKS_DIR to a "
                               f"folder elsewhere, or name the pack another id.")
    target_dir.mkdir(parents=True, exist_ok=True)
    staging = target_dir / f"{pack_id}{BUILDING}"
    staged = staging / pack_id
    _fresh_staging(staging)
    staged.mkdir()
    hashes = _write(staged, texts)
    reads, layout = sqlgen.source_reads(held, graph), sqlgen.store_layout(held, graph)
    total = built_hash(hashes, sealed(reads, layout))
    from sdg.catalog.loader import load_catalog

    try:
        load_catalog(staged / "catalog.yaml")
    except Exception as exc:  # the loader's own error, said as it is
        return BuildResult(folder=staged, pack_id=pack_id, files=hashes,
                           problems=[f"catalog.yaml does not load: {exc}"])
    problems = startup.check(staged)
    if problems:
        return BuildResult(folder=staged, pack_id=pack_id, files=hashes, problems=problems)
    (staged / MARKER).write_text(_dump({
        "source": source, "plan_hash": digest, "built_hash": total, "built_by": by,
        "built_at": datetime.now().isoformat(timespec="seconds"), "files": hashes,
        "reads": reads, "store": layout}),
        encoding="utf-8")

    swap = _Swap(staged=staged, target=target_dir / pack_id,
                 previous=previous_folder(target_dir, pack_id))
    approval.record_build(source, digest, total, install=swap.install, undo=swap.undo)
    (staging / STAGING_MARKER).unlink(missing_ok=True)
    try:
        staging.rmdir()
    except OSError:
        pass  # left behind, empty or not: a later build clears it by its marker
    return BuildResult(folder=target_dir / pack_id, pack_id=pack_id, files=hashes)


def built_folder(source: str, packs_dir: str | Path | None = None) -> Path:
    """The folder the source's plan builds: <SDG_PACKS_DIR>/<pack id>."""
    folder = sources.folder_for(source)
    held = planning.load(folder)
    return packs_folder(packs_dir) / check_pack_id((held.pack or {}).get("id"))


def read_built(source: str, name: str, packs_dir: str | Path | None = None) -> str:
    """One built file's text, by its path from the list a build writes."""
    if name not in built_files(source):
        raise BuildRefused(f"{name!r} is not a file a build writes: "
                           f"{', '.join(built_files(source))}")
    path = built_folder(source, packs_dir) / name
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not there: build the pack first")
    return path.read_text(encoding="utf-8")

