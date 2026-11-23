"""The source plan: what the person decided about a source, kept as proposal.json.

`propose` drafts it from a landscape: record kinds, then each one's levels and
features, then the tables they need. Each edit -- choose a record, select or
deselect a level, reorder, swap, state a join, name a record kind, select a
feature -- returns the plan with its tables derived again (tables.derive), so
the table list never drifts from the hierarchy, and drops it back to draft.

Edits that need the structure (choosing a record, swapping a level, stating a
join) take the key graph, rebuilt from the saved landscape by `graph_for`
without reading the source again.

Version 2 adds what the extract needs: the criteria (criteria.py), the denorm
tables with each column's privacy class (structure.py), and the pack's id
and name. Given the key graph, every edit proposes the criteria and the
structure again (`realign`), keeping the person's choices: a criterion's
selection, values and months by its id, a column's privacy and the columns
they added by (table, column). A version 1 proposal loads with them empty
and gets them on its first edit. `plan_hash` is what an approval is given to.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdg.accelerator import criteria as criteria_rules
from sdg.accelerator import hierarchy, source_read
from sdg.accelerator import structure as structure_rules
from sdg.accelerator.criteria import Criterion
from sdg.accelerator.graph import KeyGraph, SourceChecks
from sdg.accelerator.hierarchy import (FeatureCandidate, Join, LevelCandidate,
                                       RecordKindCandidate, to_dict)
from sdg.accelerator.landscape import Landscape
from sdg.accelerator.structure import DenormTable
from sdg.accelerator.tables import ROLES, SourceTable, derive
from sdg.discovery.model import PhysicalModel, now_stamp

PROPOSAL_FILE = "proposal.json"
VERSION = 2
# Versions a saved proposal may have: version 1 (phase 1) loads with no
# criteria, structure or pack, filled on its first edit.
READS_VERSIONS = (1, 2)
# A pack id: what SDG_PACK names, a folder under SDG_PACKS_DIR.
PACK_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
# Pack ids a generated pack may never take: the packs shipped with SDM, each
# a folder holding pack.json under sdg/packs.
SHIPPED_PACKS = frozenset(p.name for p in (Path(__file__).parents[1] / "packs").iterdir()
                          if (p / "pack.json").is_file())
# A record kind's name, as the person gives it: words, not markup.
NAME_MAX = 60

logger = logging.getLogger(__name__)


@dataclass
class SourcePlan:
    source: str
    read_at: str
    record_kinds: dict[str, dict[str, Any] | None] = field(
        default_factory=lambda: {"primary": None, "child": None})
    candidates: dict[str, list[RecordKindCandidate]] = field(
        default_factory=lambda: {"primary": [], "child": []})
    hierarchies: dict[str, dict[str, list]] = field(
        default_factory=lambda: {r: {"levels": [], "features": []} for r in ROLES})
    tables: list[SourceTable] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Joins the person stated, and composite keys the source confirmed: both
    # kept so the graph can be rebuilt from the saved landscape alone.
    joins: dict[str, list[Join]] = field(default_factory=lambda: {"stated": [], "found": []})
    # Phase 2: the extract's criteria by role, its denorm tables, the pack.
    criteria: dict[str, list[Criterion]] = field(
        default_factory=lambda: {r: [] for r in ROLES})
    structure: list[DenormTable] = field(default_factory=list)
    pack: dict[str, str | None] = field(default_factory=lambda: {"id": None, "name": None})
    # Link tables entered without dates whose repeated records the person
    # accepted, by role (accept_grain): the extract refuses any other.
    grain_accepted: dict[str, list[str]] = field(default_factory=lambda: {r: [] for r in ROLES})
    version: int = VERSION
    status: str = "draft"

    def levels(self, role: str) -> list[LevelCandidate]:
        return self.hierarchies.get(role, {}).get("levels", [])

    def features(self, role: str) -> list[FeatureCandidate]:
        return self.hierarchies.get(role, {}).get("features", [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "read_at": self.read_at,
            "status": self.status,
            "record_kinds": {r: (dict(k) if k else None) for r, k in self.record_kinds.items()},
            "candidates": {r: [to_dict(c) for c in cs] for r, cs in self.candidates.items()},
            "hierarchies": {r: {"levels": [to_dict(lv) for lv in h.get("levels", [])],
                                "features": [to_dict(f) for f in h.get("features", [])]}
                            for r, h in self.hierarchies.items()},
            "tables": [t.to_dict() for t in self.tables],
            "notes": list(self.notes),
            "joins": {k: [to_dict(j) for j in js] for k, js in self.joins.items()},
            "criteria": {r: [c.to_dict() for c in cs] for r, cs in self.criteria.items()},
            "structure": [t.to_dict() for t in self.structure],
            "pack": {"id": self.pack.get("id"), "name": self.pack.get("name")},
            "grain_accepted": {r: list(self.grain_accepted.get(r, [])) for r in ROLES},
        }

    def all_criteria(self) -> list[Criterion]:
        return [c for role in ROLES for c in self.criteria.get(role, [])]

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "SourcePlan":
        if held.get("version") not in READS_VERSIONS:
            raise ValueError(f"proposal version {held.get('version')!r} is not one of "
                             f"{', '.join(map(str, READS_VERSIONS))}")
        pack = held.get("pack") or {}
        return cls(
            version=VERSION,
            source=held["source"],
            read_at=held.get("read_at", ""),
            # A version 1 plan has no criteria or structure yet: never approved.
            status=held.get("status", "draft") if held.get("version") == VERSION else "draft",
            record_kinds={r: (dict(k) if k else None)
                          for r, k in (held.get("record_kinds") or {}).items()},
            candidates={r: [RecordKindCandidate.from_dict(c) for c in cs]
                        for r, cs in (held.get("candidates") or {}).items()},
            hierarchies={r: {"levels": [LevelCandidate.from_dict(lv) for lv in h.get("levels", [])],
                             "features": [FeatureCandidate.from_dict(f)
                                          for f in h.get("features", [])]}
                         for r, h in (held.get("hierarchies") or {}).items()},
            tables=[SourceTable.from_dict(t) for t in held.get("tables") or []],
            notes=list(held.get("notes") or []),
            joins={k: [Join.from_dict(j) for j in js]
                   for k, js in (held.get("joins") or {"stated": [], "found": []}).items()},
            criteria={r: [Criterion.from_dict(c) for c in (held.get("criteria") or {}).get(r, [])]
                      for r in ROLES},
            structure=[DenormTable.from_dict(t) for t in held.get("structure") or []],
            pack={"id": pack.get("id"), "name": pack.get("name")},
            grain_accepted=_grain_accepted(held.get("grain_accepted")),
        )


def _grain_accepted(held: Any) -> dict[str, list[str]]:
    """The saved grain acceptances: a list of table names per role."""
    held = held or {}
    if not isinstance(held, dict) or not all(
            isinstance(v, list) and all(isinstance(t, str) for t in v) for v in held.values()):
        raise ValueError("a saved grain acceptance is a list of table names per role")
    return {r: list(held.get(r) or []) for r in ROLES}


# --- reading the source, when it can be read ------------------------------

def pair_counter(model: PhysicalModel, checks: SourceChecks) -> hierarchy.PairCounts:
    """source_read.pair_counts for order_levels, each pair counted once and
    within the run's checks: a pair the cap leaves out or the database will
    not count is noted there and left unsettled."""
    held: dict[tuple[str, str, str], tuple[int, int, int] | None] = {}

    def counts(table: str, a: str, b: str) -> tuple[int, int, int] | None:
        if (table, a, b) not in held:
            found = model.table(table)
            if found is None:
                held[(table, a, b)] = None
            else:
                checked, value = checks.run(
                    f"whether {b} nests under {a} on {table}",
                    lambda: source_read.pair_counts(checks.registry, found, a, b))
                held[(table, a, b)] = value if checked else None
        return held[(table, a, b)]

    return counts


def graph_for(landscape: Landscape, plan: SourcePlan | None = None, *,
              registry: Any = None, checks: SourceChecks | None = None) -> KeyGraph:
    """The key graph of a landscape, with the plan's stated and confirmed
    joins. Without a registry nothing is sent to the source."""
    stated = [j.to_edge() for j in plan.joins.get("stated", [])] if plan else []
    found = [j.to_edge() for j in plan.joins.get("found", [])] if plan else []
    return KeyGraph.from_landscape(landscape, registry=registry, stated=stated, confirmed=found,
                                   checks=checks)


# --- proposing ------------------------------------------------------------

def _require_table(graph: KeyGraph, table: str) -> None:
    found = graph.table(table)
    if found is None:
        raise ValueError(f"the source has no table {table!r}")
    if found.row_count <= 0:
        raise ValueError(f"{table} holds no rows, so it cannot be a record kind")


def _candidate_for(graph: KeyGraph, candidates: list[RecordKindCandidate],
                   table: str) -> RecordKindCandidate:
    held = next((c for c in candidates if c.table == table), None)
    if held is None:
        _require_table(graph, table)
        lines = graph.lines_of(table)
        held = RecordKindCandidate(
            table=table, score=0.0, reasons=["chosen by the person"], lines_table=lines,
            lines_key=list(graph.table(lines).primary_key) if lines else [])
        candidates.append(held)
    return held


def _primary_order(plan: SourcePlan) -> list[str]:
    return [lv.id for lv in sorted(plan.levels("primary"), key=lambda lv: lv.order)
            if lv.selected]


def _set_record(plan: SourcePlan, graph: KeyGraph, role: str, table: str | None,
                pair_counts: hierarchy.PairCounts | None) -> None:
    if table is None:
        plan.record_kinds[role] = None
        plan.hierarchies[role] = {"levels": [], "features": []}
        return
    candidate = _candidate_for(graph, plan.candidates.setdefault(role, []), table)
    primary = plan.record_kinds["primary"]["table"] if role == "child" else None
    plan.record_kinds[role] = {
        "table": table,
        "key_columns": hierarchy.key_columns(graph, table),
        "lines_table": candidate.lines_table,
        "name": {"one": "", "many": ""},
    }
    plan.hierarchies[role] = {
        "levels": _levels(graph, table, candidate.lines_table, primary,
                          _primary_order(plan) if role == "child" else [], pair_counts,
                          plan.notes),
        "features": hierarchy.propose_features(graph, table, lines=candidate.lines_table),
    }


def _levels(graph: KeyGraph, record: str, lines: str | None, primary: str | None,
            first: list[str], pair_counts: hierarchy.PairCounts | None,
            notes: list[str] | None = None) -> list[LevelCandidate]:
    levels = hierarchy.propose_levels(graph, record, lines=lines, primary=primary, notes=notes)
    levels = hierarchy.order_levels(levels, graph, pair_counts, first=first)
    levels = hierarchy.select_defaults(levels)
    return hierarchy.flag_nesting(levels, graph, pair_counts)


def _choose_child(plan: SourcePlan, graph: KeyGraph, child: str | None,
                  pair_counts: hierarchy.PairCounts | None) -> None:
    primary = plan.record_kinds["primary"]["table"]
    plan.candidates["child"] = hierarchy.propose_child(graph, primary)
    if child is not None and child == primary:
        raise ValueError("the child record kind cannot be the primary one")
    chosen = child or (plan.candidates["child"][0].table if plan.candidates["child"] else None)
    if chosen is None:
        plan.notes.append(f"No table keys to {primary} with more than one row each: "
                          f"no child record kind was proposed.")
    _set_record(plan, graph, "child", chosen, pair_counts)


def landscape_stamp(landscape: Landscape) -> str:
    """When the landscape was read, as a proposal made from it keeps it."""
    return landscape.profile.profiled or landscape.model.discovered or ""


STALE = "The landscape was read again after this proposal; run propose --force."


def refuse_stale(plan: SourcePlan, landscape: Landscape) -> None:
    """Refuse a proposal made from an earlier read of the landscape: its
    levels and figures are the old read's, and an edit would trust them."""
    stamp = landscape_stamp(landscape)
    if stamp and plan.read_at != stamp:
        raise ValueError(STALE)


def propose(landscape: Landscape, *, primary: str | None = None, child: str | None = None,
            registry: Any = None, check_seconds: float | None = None) -> SourcePlan:
    """Draft a plan from a landscape: the person's record kinds or the top
    candidates, each one's levels and features, and the tables they need.

    With `registry` (a SourceRegistry) composite keys and open nestings are
    checked against the source through source_read, within `check_seconds`
    in all (graph.CHECK_SECONDS unless given); without it the draft is made
    from the saved figures alone and sends nothing. What could not be
    checked -- refused, past the run's cap or its time -- is in the plan's
    notes."""
    checks = None
    if registry is not None:
        checks = SourceChecks(registry)
        if check_seconds is not None:
            if check_seconds <= 0:
                raise ValueError("the time limit on the source checks must be above zero")
            checks.seconds = float(check_seconds)
    graph = graph_for(landscape, registry=registry, checks=checks)
    counts = pair_counter(landscape.model, checks) if checks is not None else None
    plan = SourcePlan(source=landscape.name,
                      read_at=landscape_stamp(landscape) or now_stamp())
    plan.notes += graph.notes
    plan.joins["found"] = [Join.up(e) for e in graph.confirmed]
    plan.candidates["primary"] = hierarchy.propose_primary(graph)
    chosen = primary or (plan.candidates["primary"][0].table
                         if plan.candidates["primary"] else None)
    if chosen is None:
        plan.notes.append("No table holds rows: nothing to propose.")
    else:
        _set_record(plan, graph, "primary", chosen, counts)
        _choose_child(plan, graph, child, counts)
    if checks is not None:
        plan.notes += checks.notes()
    login = getattr(registry, "login", None) or {}
    if login.get("can_write"):
        can = ", ".join(p for p, yes in login.get("permissions", {}).items() if yes)
        plan.notes.append(f"Read with a login that can {can} in the source, as the person "
                          f"allowed; only reads were sent.")
    plan.notes += bridge_notes(plan, graph)
    plan.tables = derive(plan)
    return realign(plan, graph)


def bridge_notes(plan: SourcePlan, graph: KeyGraph) -> list[str]:
    """A level reached through a dated link table holds several link rows per
    record; until the criteria step keeps the one in force by its start and
    end dates, records would repeat. Said once per bridge and level."""
    out = []
    for role in ROLES:
        for level in plan.levels(role):
            for join in level.path:
                down = any(e.child_table == join.to_table and e.parent_table == join.from_table
                           and e.child_columns == join.to_columns
                           for e in graph.edges_to(join.from_table))
                if down:
                    pairs = graph.date_pairs(join.to_table)
                    dates = f" ({pairs[0][0]} to {pairs[0][1]})" if pairs else ""
                    out.append(f"{role} level {level.id} is reached through {join.to_table}, "
                               f"a dated link{dates}: the criteria step must keep the row in "
                               f"force, or records repeat.")
    return out


# --- saving ---------------------------------------------------------------

def save(plan: SourcePlan, folder: str | Path) -> Path:
    path = Path(folder) / PROPOSAL_FILE
    # Written beside it, then put in its place in one step: a crash part way
    # never leaves a half-written plan.
    staged = path.with_name(path.name + ".saving")
    staged.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(staged, path)
    return path


def load(folder: str | Path) -> SourcePlan:
    path = Path(folder) / PROPOSAL_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} is not there: run `sdg accelerate propose` first")
    return SourcePlan.from_dict(json.loads(path.read_text(encoding="utf-8")))


# --- edits ----------------------------------------------------------------

def realign(plan: SourcePlan, graph: KeyGraph) -> SourcePlan:
    """Propose the criteria and the denorm structure again from the plan as
    it stands, keeping what the person decided: each criterion's selection,
    values and months by its id, and each column's privacy and the columns
    they added by (table, column)."""
    old = plan.all_criteria()
    fresh = criteria_rules.carry(criteria_rules.propose(plan, graph), old)
    plan.criteria = {r: [c for c in fresh if c.role == r] for r in ROLES}
    plan.structure = structure_rules.propose(plan, graph, plan.structure)
    # An acceptance holds only while its link still grows the grain.
    plan.grain_accepted = {
        r: [t for t in plan.grain_accepted.get(r, [])
            if plan.record_kinds.get(r) and t in structure_rules.unpicked_links(plan, r, graph)]
        for r in ROLES}
    return plan


def _touched(plan: SourcePlan, graph: KeyGraph | None = None) -> SourcePlan:
    """Every edit ends here: back to draft, tables derived again, and, given
    the key graph, the criteria and structure proposed again (realign). The
    command line and the screen realign after every edit they make."""
    plan.status = "draft"
    plan.tables = derive(plan)
    return realign(plan, graph) if graph is not None else plan


def plan_hash(plan: SourcePlan) -> str:
    """SHA-256 of the plan's content, without its status and notes: what an
    approval is given to."""
    held = plan.to_dict()
    held.pop("status", None)
    held.pop("notes", None)
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode("utf-8")).hexdigest()


def _role(plan: SourcePlan, role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"{role!r} is not a role: use primary or child")
    kind = plan.record_kinds.get(role)
    if not kind:
        raise ValueError(f"the plan has no {role} record kind")
    return kind


def _level(plan: SourcePlan, role: str, level_id: str) -> LevelCandidate:
    _role(plan, role)
    found = next((lv for lv in plan.levels(role) if lv.id == level_id), None)
    if found is None:
        raise ValueError(f"the {role} record kind has no level {level_id!r}; "
                         f"`sdg accelerate show` lists them")
    return found


def _reflag(plan: SourcePlan, role: str, graph: KeyGraph | None) -> None:
    if graph is not None:
        hierarchy.flag_nesting(plan.levels(role), graph)


def _carry(plan: SourcePlan, role: str, kind: dict[str, Any],
           levels: list[LevelCandidate], features: list[FeatureCandidate],
           graph: KeyGraph) -> None:
    """Keep what the person decided about a record kind that stays: its
    name, and the selection and order of every level and feature still
    proposed. A level new to it comes in unselected, after the rest."""
    plan.record_kinds[role]["name"] = dict(kind.get("name") or {"one": "", "many": ""})
    held = {lv.id: lv for lv in levels}
    last = max((lv.order for lv in levels), default=0)
    for level in plan.levels(role):
        old = held.get(level.id)
        if old is not None:
            level.selected, level.order = old.selected, old.order
            level.nesting = {**old.nesting, **level.nesting}
        else:
            level.selected, level.order = False, last + level.order
    plan.hierarchies[role]["levels"] = hierarchy.renumber(plan.levels(role))
    hierarchy.flag_nesting(plan.levels(role), graph)
    chosen = {f.id: f.selected for f in features}
    for feature in plan.features(role):
        feature.selected = chosen.get(feature.id, feature.selected)


def choose_record(plan: SourcePlan, role: str, table: str, *, graph: KeyGraph) -> SourcePlan:
    """Make `table` the role's record kind and propose its levels again;
    nothing changes when it already is. A new primary proposes the child
    again too, keeping the child -- its name, levels and features as the
    person left them -- when it still keys to the new primary."""
    if role not in ROLES:
        raise ValueError(f"{role!r} is not a role: use primary or child")
    if table == (plan.record_kinds.get(role) or {}).get("table"):
        return plan
    _require_table(graph, table)
    if role == "primary":
        old = plan.record_kinds.get("child")
        child = (old or {}).get("table")
        levels, features = list(plan.levels("child")), list(plan.features("child"))
        _set_record(plan, graph, "primary", table, None)
        keeps = child is not None and child != table and any(
            e.child_table == child for e in graph.edges_to(table))
        _choose_child(plan, graph, child if keeps else None, None)
        if keeps and old:
            _carry(plan, "child", old, levels, features, graph)
    else:
        if not plan.record_kinds.get("primary"):
            raise ValueError("choose the primary record kind first")
        if table == plan.record_kinds["primary"]["table"]:
            raise ValueError("the child record kind cannot be the primary one")
        _set_record(plan, graph, "child", table, None)
    return _touched(plan, graph)


def select_level(plan: SourcePlan, role: str, level_id: str, on: bool, *,
                 graph: KeyGraph | None = None) -> SourcePlan:
    """Select or deselect a level. A newly selected walk level goes after the
    selected walk levels, a narrowing one after every selected level."""
    level = _level(plan, role, level_id)
    if on and not level.selected:
        chosen = [lv for lv in plan.levels(role) if lv.selected]
        after = [lv.order for lv in chosen if lv.kind == "walk"] if level.kind == "walk" else []
        after = after or [lv.order for lv in chosen]
        level.order = (max(after) if after else 0) + 0.5
        level.selected = True
    elif not on:
        level.selected = False
    plan.hierarchies[role]["levels"] = hierarchy.renumber(plan.levels(role))
    _reflag(plan, role, graph)
    return _touched(plan, graph)


def reorder_levels(plan: SourcePlan, role: str, ids: list[str], *,
                   graph: KeyGraph | None = None) -> SourcePlan:
    """Put the selected levels named in `ids` first, in that order; the other
    selected levels follow as they were. Changes no table."""
    if len(set(ids)) != len(ids):
        raise ValueError("a level is named twice")
    named = [_level(plan, role, i) for i in ids]
    unselected = [lv.id for lv in named if not lv.selected]
    if unselected:
        raise ValueError(f"only selected levels are ordered; select first: {', '.join(unselected)}")
    rest = sorted((lv for lv in plan.levels(role) if lv.id not in set(ids)),
                  key=lambda lv: (not lv.selected, lv.order))
    for number, level in enumerate(named + rest, start=1):
        level.order = number
    plan.hierarchies[role]["levels"] = hierarchy.renumber(plan.levels(role))
    _reflag(plan, role, graph)
    return _touched(plan, graph)


def swap_level(plan: SourcePlan, role: str, level_id: str, table: str, column: str, *,
               graph: KeyGraph) -> SourcePlan:
    """Replace a level by another table's column, reached by its own path;
    it keeps the old level's place and selection. Refused for anything
    propose_levels would not offer (hierarchy.level_refusal), and for a
    column that is already another of the record kind's levels."""
    old = _level(plan, role, level_id)
    kind = plan.record_kinds[role]
    record = kind["table"]
    found = graph.table(table)
    if found is None:
        raise ValueError(f"the source has no table {table!r}")
    if found.column(column) is None:
        raise ValueError(f"{table} has no column {column!r}")
    if graph.is_personal(table, column):
        raise ValueError(f"{table}.{column} holds personal values and cannot be a level")
    reached = hierarchy.path_to(graph, record, table)
    if reached is None:
        raise ValueError(f"no keys lead from {record} to {table}: state the join first")
    path, through = reached
    primary = plan.record_kinds["primary"]["table"] if role == "child" else None
    refused = hierarchy.level_refusal(graph, record, table, column, path,
                                      lines=kind.get("lines_table"), primary=primary)
    if refused:
        raise ValueError(refused)
    new_id = hierarchy.level_id(table, column)
    if new_id != old.id and any(lv.id == new_id for lv in plan.levels(role)):
        raise ValueError(f"{new_id} is already a level of the {role} record kind: select it, "
                         f"and deselect {old.id}, instead")
    new = hierarchy.level_for(graph, record, table, column, path, bridge=through)
    if new is None:
        raise ValueError(f"{table}.{column} has fewer than two values")
    new.selected, new.order = old.selected, old.order
    new.reason = f"swapped in for {old.column}; {new.reason}"
    plan.hierarchies[role]["levels"] = [new if lv.id == old.id else lv
                                        for lv in plan.levels(role)]
    plan.hierarchies[role]["levels"] = hierarchy.renumber(plan.levels(role))
    _reflag(plan, role, graph)
    return _touched(plan, graph)


def state_join(plan: SourcePlan, role: str, join: Join, *, graph: KeyGraph) -> SourcePlan:
    """Add a join inference did not find, then propose the role's levels
    again: levels already there keep their selection and order, new ones
    come in unselected after them."""
    kind = _role(plan, role)
    graph.add_stated(join.to_edge())
    join = Join(join.from_table, list(join.from_columns), join.to_table, list(join.to_columns),
                "stated", None)
    stated = plan.joins.setdefault("stated", [])
    primary_table = (plan.record_kinds.get("primary") or {}).get("table")
    if role == "child" and join.from_table == kind["table"] and join.to_table == primary_table:
        # The one join the child follows to its primary: stating another
        # replaces it, so exactly one is ever named (the graph is built again
        # from the plan's joins on the next edit).
        stated[:] = [j for j in stated
                     if not (j.from_table == join.from_table and j.to_table == join.to_table)]
    if join not in stated:
        stated.append(join)
    primary = plan.record_kinds["primary"]["table"] if role == "child" else None
    fresh = hierarchy.propose_levels(graph, kind["table"], lines=kind.get("lines_table"),
                                     primary=primary, notes=plan.notes)
    held = {lv.id: lv for lv in plan.levels(role)}
    last = max((lv.order for lv in held.values()), default=0)
    for level in hierarchy.order_levels([lv for lv in fresh if lv.id not in held], graph):
        level.selected = False
        level.order = last + level.order
        held[level.id] = level
    plan.hierarchies[role]["levels"] = hierarchy.renumber(list(held.values()))
    _reflag(plan, role, graph)
    return _touched(plan, graph)


def _plain_words(text: str, what: str) -> str:
    words = " ".join((text or "").split())
    if not words:
        raise ValueError(f"{what} is empty")
    if len(words) > NAME_MAX or not all(ch.isalnum() or ch in " -_'&/" for ch in words):
        raise ValueError(f"{what} must be plain words, {NAME_MAX} characters at most")
    return words


def name_record(plan: SourcePlan, role: str, one: str, many: str, *,
                graph: KeyGraph | None = None) -> SourcePlan:
    """Name a record kind, one and many: the person's words for it. The many
    name names the record kind's denorm table."""
    kind = _role(plan, role)
    kind["name"] = {"one": _plain_words(one, "the name of one"),
                    "many": _plain_words(many, "the name of many")}
    return _touched(plan, graph)


def select_feature(plan: SourcePlan, role: str, feature_id: str, on: bool, *,
                   graph: KeyGraph | None = None) -> SourcePlan:
    _role(plan, role)
    found = next((f for f in plan.features(role) if f.id == feature_id), None)
    if found is None:
        raise ValueError(f"the {role} record kind has no feature {feature_id!r}")
    found.selected = on
    return _touched(plan, graph)


# --- phase 2 edits: criteria, structure, pack -------------------------------

def _criterion(plan: SourcePlan, criterion_id: str) -> Criterion:
    found = next((c for c in plan.all_criteria() if c.id == criterion_id), None)
    if found is None:
        raise ValueError(f"the plan has no criterion {criterion_id!r}; "
                         f"`sdg accelerate criteria` lists them")
    return found


def select_criterion(plan: SourcePlan, criterion_id: str, on: bool, *,
                     graph: KeyGraph) -> SourcePlan:
    """Select or deselect a criterion. A values criterion is selected only
    once it has values."""
    found = _criterion(plan, criterion_id)
    if on:
        criteria_rules.check(found, graph)
        if found.kind == "values" and not found.values:
            raise ValueError(f"{criterion_id} has no values to keep: set them first")
    found.selected = on
    return _touched(plan, graph)


def set_criterion(plan: SourcePlan, criterion_id: str, values: list[str] | None = None,
                  months: int | None = None, *, op: str | None = None,
                  graph: KeyGraph) -> SourcePlan:
    """Set a values criterion's values (typed by the person, each checked as
    a literal for its column) or op, or a window's months. A share measured
    before no longer holds and is cleared."""
    found = _criterion(plan, criterion_id)
    if values is None and months is None and op is None:
        raise ValueError("nothing to set: give values, an op or months")
    changed = Criterion.from_dict(found.to_dict())
    if (values is not None or op is not None) and found.kind != "values":
        raise ValueError(f"{criterion_id} is not a values criterion")
    if months is not None and found.kind != "window":
        raise ValueError(f"{criterion_id} is not a window")
    if values is not None:
        cleaned = list(dict.fromkeys(criteria_rules.clean_value(v) for v in values))
        if not cleaned:
            raise ValueError("give at least one value; deselect the criterion to drop it")
        changed.values = cleaned
    if op is not None:
        changed.op = op
    if months is not None:
        changed.months = months
    criteria_rules.check(changed, graph)
    found.values, found.months, found.op = changed.values, changed.months, changed.op
    found.share_kept = None
    return _touched(plan, graph)


def set_privacy(plan: SourcePlan, table: str, column: str, privacy: str, *,
                graph: KeyGraph, confirm_not_personal: bool = False) -> SourcePlan:
    """Set a column's privacy class in every denorm table holding it. A
    locked column is never made kept; a column the two tables join on is
    never withheld; a key that may say who a row is about is kept only with
    `confirm_not_personal`, the person's word that it holds nothing personal
    (structure.refusal). The confirmation is kept on the column while it is
    kept, and dropped when it is made anything else."""
    found = structure_rules.find(plan.structure, table, column)
    if not found:
        raise ValueError(f"{table}.{column} is in no denorm table; "
                         f"`sdg accelerate structure` lists them")
    if confirm_not_personal and privacy != "kept":
        raise ValueError("only a column being kept is confirmed as holding nothing personal")

    def refused(held: structure_rules.DenormColumn) -> str | None:
        return structure_rules.refusal(
            held, privacy, graph, confirmed=confirm_not_personal or held.confirmed_not_personal)

    for held in found:
        said = refused(held)
        if said:
            raise ValueError(said)
    # The two sides of a join take one class, or the join finds nothing.
    for other in structure_rules.partners(plan, graph, table, column):
        for held in structure_rules.find(plan.structure, *other):
            said = refused(held)
            if said:
                raise ValueError(said)
            found.append(held)
    for held in found:
        source = graph.table(held.source_table).column(held.source_column)
        held.confirmed_not_personal = privacy == "kept" and (
            confirm_not_personal or held.confirmed_not_personal)
        held.privacy = privacy
        held.sql_type = (structure_rules.SURROGATE_TYPE if privacy == "surrogate"
                         else source.type_description())
    return _touched(plan, graph)


def add_extra_column(plan: SourcePlan, role: str, table: str, column: str, *,
                     graph: KeyGraph) -> SourcePlan:
    """Add a column to the role's denorm table, from a table the role
    already reads: its record, lines, a level's, a label's or a bridge's."""
    _role(plan, role)
    reads = {t.table for t in plan.tables if any(s["role"] == role for s in t.serves)}
    if table not in reads:
        raise ValueError(f"the {role} record kind does not read {table}; it reads "
                         f"{', '.join(sorted(reads))}")
    found = graph.table(table)
    if found is None or found.column(column) is None:
        raise ValueError(f"{table} has no column {column!r}")
    plan.structure = structure_rules.propose(plan, graph, plan.structure)
    denorm = next(t for t in plan.structure if t.role == role)
    if any(c.source_table == table and c.source_column == column for c in denorm.columns):
        raise ValueError(f"{table}.{column} is in {denorm.name} already")
    privacy, reason, locked = structure_rules.privacy_for(graph, table, column, "extra")
    denorm.columns.append(structure_rules.DenormColumn(
        name=column, source_table=table, source_column=column,
        sql_type=found.column(column).type_description(), serves="extra", privacy=privacy,
        proposed=privacy, reason=reason, locked=locked))
    # Proposed again at once: the name is made plain and a clash prefixed.
    return _touched(plan, graph)


def accept_grain(plan: SourcePlan, role: str, table: str, on: bool, *,
                 graph: KeyGraph) -> SourcePlan:
    """Accept, or take back, that the role's denorm table repeats each record
    once per row of `table`: a link a selected level enters with no dates to
    pick a row by. The extract refuses such a link until it is accepted."""
    _role(plan, role)
    links = structure_rules.unpicked_links(plan, role, graph)
    held = plan.grain_accepted.setdefault(role, [])
    if on:
        if table not in links:
            raise ValueError(f"{table} is not a link the {role} record kind enters without "
                             f"dates, so there is no grain change to accept"
                             + (f"; it enters {', '.join(links)}" if links else ""))
        if table not in held:
            held.append(table)
            held.sort()
    elif table in held:
        held.remove(table)
    return _touched(plan, graph)


def name_pack(plan: SourcePlan, pack_id: str, name: str) -> SourcePlan:
    """Name the pack the plan builds: its id (its folder under SDG_PACKS_DIR,
    what SDG_PACK names) and the name a person reads."""
    held = (pack_id or "").strip()
    if not PACK_ID.fullmatch(held):
        raise ValueError("a pack id is 2 to 32 lowercase letters, digits or _, starting "
                         "with a letter")
    if held in SHIPPED_PACKS:
        raise ValueError(f"{held} is a pack shipped with SDM: choose another id")
    plan.pack = {"id": held, "name": _plain_words(name, "the pack's name")}
    plan.status = "draft"
    return plan
