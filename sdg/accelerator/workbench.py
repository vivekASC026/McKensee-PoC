"""What the Source setup screen asks of the accelerator.

The screen does what the commands do -- name a source, read it, propose,
edit the proposal -- and this module is the one place it asks, so the API's
routes stay thin and every rule stays here: the same refusals the commands
give (a login that can write, a proposal older than the landscape, a level
that cannot be one), in the same words.

Two things are the screen's own. A source can be added by name for the
running session, beside the ones named in .env; and a plan is handed back
with the alternatives each level could be swapped for, worked out from the
saved landscape alone, so choosing one is a single step.

Nothing here reads a value of any column: what reaches the screen is names
of tables and columns, counts, and the labels the engine wrote.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sdg.accelerator import approval, builder, hierarchy, landscape, plan, sources
from sdg.accelerator import criteria as criteria_rules
from sdg.accelerator import structure as structure_rules
from sdg.accelerator.graph import KeyGraph, SourceChecks, connection_lost
from sdg.accelerator.plan import SourcePlan
from sdg.accelerator.source_read import error_said
from sdg.accelerator.tables import ROLES
from sdg.config import get_settings, load_env
from sdg.discovery.errors import is_database_error

# The parts of a source's settings in .env, SDG_SOURCE_<NAME>_<PART>: a name
# is what is left once the prefix and one of these are taken off.
ENV_PREFIX = "SDG_SOURCE_"
ENV_PARTS = ("TRUST_CERT", "DATABASE", "PASSWORD", "TRUSTED", "ENCRYPT", "SERVER", "DRIVER",
             "PORT", "USER")

# Alternatives offered for a role's levels, most confident first.
ALTERNATIVES_SHOWN = 20

# Sources added on screen: held by this process only, gone on a restart.
_added: set[str] = set()
# One change to a source's saved files at a time: an edit's load, change and
# save are one step, and a job's saves wait for it (and it for them), so an
# edit never writes back a plan a job has just replaced.
_editing = threading.Lock()


class Conflict(Exception):
    """A request the state of the source refuses: the landscape not read yet,
    a proposal older than it, a proposal that would be replaced unasked, a
    plan not ready to submit, a pack not built yet. `kind` names which, for
    the screen: not_read, no_proposal, stale, proposal_saved, not_ready,
    changed, packs_dir, pack_id, not_built, unknown_file. `status` is the
    answer's (409 unless said)."""

    def __init__(self, message: str, kind: str | None = None, status: int = 409):
        super().__init__(message)
        self.kind = kind
        self.status = status


class StoreFailed(Exception):
    """The store (where approvals are kept) could not be reached or written:
    said by the error's kind and codes only."""


# What the screen says for a proposal older than the landscape. The engine's
# own words (plan.STALE) name the command to run; the screen has a button.
STALE_SAID = ("This proposal was made from an earlier read of the source, so it cannot be "
              "changed. Propose again to use the new read; the choices made in it are replaced.")

# The parts of the engine's messages that speak to the command line: taken
# out of what the screen is told (screen_words), the rest kept as said.
CLI_FRAGMENTS = (
    "; `sdg accelerate show` lists them",
    " (--allow-write-login)",
    ", or pass --allow-write-login to go on with this one",
    "; run propose --force",
    "; `sdg accelerate criteria` lists them",
    "; `sdg accelerate structure` lists them",
)


def screen_words(message: str) -> str:
    """An engine message as the screen says it: without the command-line
    advice it gives (the screen offers its own buttons)."""
    for fragment in CLI_FRAGMENTS:
        message = message.replace(fragment, "")
    return message


# --- naming sources ---------------------------------------------------------

def named_in_env() -> list[str]:
    """The sources named by a setting in .env (or the environment)."""
    load_env()
    found: set[str] = set()
    for key in os.environ:
        upper = key.upper()
        if not upper.startswith(ENV_PREFIX):
            continue
        rest = upper[len(ENV_PREFIX):]
        for part in ENV_PARTS:
            if rest.endswith("_" + part):
                try:
                    found.add(sources.plain_name(rest[:-len(part) - 1]))
                except sources.SourceError:
                    pass  # a setting that names no source: not one to list
                break
    return sorted(found)


def _accelerator_folder() -> Path:
    return get_settings().work_dir / "accelerator"


def _read_before() -> list[str]:
    """Sources with a landscape saved under the work folder."""
    parent = _accelerator_folder()
    if not parent.is_dir() or sources._redirected(parent):
        return []
    found = []
    for folder in parent.iterdir():
        try:
            name = sources.plain_name(folder.name)
        except sources.SourceError:
            continue
        if folder.is_dir() and not sources._redirected(folder) and (
                folder / landscape.SUMMARY_FILE).exists():
            found.append(name)
    return found


def _login_kind(name: str) -> str:
    if os.getenv(f"SDG_SOURCE_{name}_USER", "").strip():
        return "its own login"
    if os.getenv(f"SDG_SOURCE_{name}_TRUSTED", "").strip():
        return "Windows sign-in"
    return "the configured login"


def describe(name: str, origins: list[str]) -> dict[str, Any]:
    """A source as the screen lists it: where it is, how it signs in (never
    the login's name or password), and what has been done with it."""
    name = sources.plain_name(name)
    entry: dict[str, Any] = {"name": name, "origins": origins, "database": None, "server": None,
                             "login": _login_kind(name), "problem": None,
                             "read": False, "read_on": None, "proposed": False}
    try:
        settings, database = sources.source_settings(name)
        entry["database"], entry["server"] = database, settings.server
    except sources.SourceError as exc:
        entry["problem"] = str(exc)
    folder = _accelerator_folder() / name
    if folder.is_dir() and not sources._redirected(folder):
        summary = folder / landscape.SUMMARY_FILE
        if summary.exists():
            entry["read"] = True
            entry["read_on"] = datetime.fromtimestamp(
                summary.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        entry["proposed"] = (folder / plan.PROPOSAL_FILE).exists()
    return entry


def list_sources() -> list[dict[str, Any]]:
    """Every source the screen offers: named in .env, added this session, or
    read before; each once, by name."""
    origins: dict[str, list[str]] = {}
    for origin, names in (("env", named_in_env()), ("added", sorted(_added)),
                          ("read", _read_before())):
        for name in names:
            origins.setdefault(name, []).append(origin)
    return [describe(name, held) for name, held in sorted(origins.items())]


def add_source(name: str) -> list[dict[str, Any]]:
    """Add a source by name for this session. Its connection comes from .env
    as any source's does; refused when the name is not a plain one, or when
    this machine needs the source's own login and none is set."""
    name = sources.plain_name(name)
    sources.source_settings(name)
    _added.add(name)
    return list_sources()


def unreadable(name: str, exc: BaseException) -> str:
    """A database error, said by its kind and codes only: the driver's own
    words can quote a value from the source."""
    why = (" The connection failed or was refused: check the server, the login and "
           "SDG_SOURCE_<NAME>_* in .env." if connection_lost(exc) else "")
    return f"{name} could not be read ({error_said(exc)}).{why}"


# --- reading and proposing --------------------------------------------------

def open_source(name: str, *, allow_write_login: bool = False) -> sources.SourceRegistry:
    """A registry reading the source, its login checked first (refused with
    sources.WriteLogin when it can write, unless `allow_write_login`)."""
    return sources.registry_for(sources.plain_name(name), allow_write_login=allow_write_login)


def has_proposal(name: str) -> bool:
    return (sources.folder_for(name) / plan.PROPOSAL_FILE).exists()


Stage = Callable[[int, int, str], None]

STAGE_WORDS = {
    "structure": "Reading tables, columns and keys",
    "profile": "Profiling the tables that matter",
    "propose": "Proposing record kinds and levels",
}


def read_source(name: str, registry: sources.SourceRegistry, stage: Stage, *,
                minutes: float | None = None) -> dict[str, Any]:
    """Read the landscape and save it; then, when the source has no proposal
    yet, propose from it and save that too. A proposal already there is kept,
    with every choice made in it: proposing again is the person's call.
    `stage(done, total, words)` is told each stage as it starts."""
    name = sources.plain_name(name)
    proposing = not has_proposal(name)
    total = 3 if proposing else 2
    steps = {"structure": 0, "profile": 1}
    read = landscape.read(name, registry=registry, max_minutes=minutes,
                          on_stage=lambda what: stage(steps[what], total, STAGE_WORDS[what]))
    if proposing:
        stage(2, total, STAGE_WORDS["propose"])
        drafted = plan.propose(read, registry=registry)
    with _editing:
        folder = landscape.save(read)
        if proposing:
            plan.save(drafted, folder)
    stage(total, total, "Done")
    return {"source": name, "summary": summary_view(read.summary), "proposed": proposing}


def refuse_replacing(name: str, force: bool) -> None:
    if has_proposal(name) and not force:
        raise Conflict(f"A proposal is saved for {name}, with the choices made in it. "
                       f"Proposing again replaces it: its selections and names are lost.",
                       "proposal_saved")


def saved_landscape(name: str) -> landscape.Landscape:
    try:
        return landscape.load(name)
    except FileNotFoundError as exc:
        raise Conflict(f"{sources.plain_name(name)} has not been read yet: read it under "
                       f"Sources first.", "not_read") from exc


def propose_source(name: str, registry: sources.SourceRegistry | None,
                   stage: Stage) -> dict[str, Any]:
    """Propose from the saved landscape and save it, replacing any proposal
    (refuse_replacing is asked first, by the caller, before the work starts)."""
    read = saved_landscape(name)
    stage(0, 1, STAGE_WORDS["propose"])
    held = plan.propose(read, registry=registry)
    with _editing:
        plan.save(held, sources.folder_for(name))
    stage(1, 1, "Done")
    return {"source": read.name, "proposed": True}


# --- the landscape, as the screen shows it ------------------------------------

def summary_view(summary: dict[str, Any]) -> dict[str, Any]:
    """The summary's counts and names, each key there even when a landscape
    saved by an earlier version lacks it."""
    login = summary.get("login") or {}
    return {
        "tables": int(summary.get("tables") or 0),
        "columns": int(summary.get("columns") or 0),
        "declared_keys": int(summary.get("declared_keys") or 0),
        "inferred_keys": int(summary.get("inferred_keys") or 0),
        "shortlisted": len(summary.get("shortlisted") or []),
        "skipped": [{"table": s.get("table", ""), "analysis": s.get("analysis", ""),
                     "reason": s.get("reason", "")} for s in summary.get("skipped") or []],
        "hubs": [{"table": h.get("table", ""), "referenced_by": int(h.get("referenced_by") or 0),
                  "rows": int(h.get("rows") or 0)} for h in summary.get("hubs") or []],
        "code_tables": list(summary.get("code_tables") or []),
        "date_pair_tables": {t: [{"start": p.get("start", ""), "end": p.get("end", ""),
                                  "placeholders": p.get("placeholders")} for p in pairs]
                             for t, pairs in (summary.get("date_pair_tables") or {}).items()},
        "person_like_tables": {t: list(cs) for t, cs in
                               (summary.get("person_like_tables") or {}).items()},
        "login_can_write": bool(login.get("can_write")),
        "seconds": summary.get("seconds"),
    }


def landscape_summary(name: str) -> dict[str, Any] | None:
    """The saved landscape's summary, or None when the source was never read."""
    folder = sources.folder_for(name)
    path = folder / landscape.SUMMARY_FILE
    if not path.exists():
        return None
    return summary_view(json.loads(path.read_text(encoding="utf-8")))


# --- the proposal ---------------------------------------------------------------

def alternatives(held: SourcePlan, role: str, graph: KeyGraph) -> list[dict[str, Any]]:
    """What a level of the role could be swapped for: each column swap_level
    would take that is not already one of the role's levels, from tables
    reached by keys (the key the path arrives by, or a code column), most
    confident first. The same rule as swap_level's, so every one offered is
    accepted."""
    kind = held.record_kinds.get(role)
    if not kind:
        return []
    record, lines = kind["table"], kind.get("lines_table")
    primary = (held.record_kinds.get("primary") or {}).get("table") if role == "child" else None
    taken = {lv.id for lv in held.levels(role)}
    paths = hierarchy.reachable(graph, record)
    bridges = hierarchy.bridged(graph, record, paths)
    reached = [(t, p, False) for t, p in paths.items()] + [
        (t, p, True) for t, p in bridges.items() if t not in paths]
    found: list[hierarchy.LevelCandidate] = []
    for table, path, through in reached:
        columns = [path[-1].to_columns[0]] if path and len(path[-1].to_columns) == 1 else []
        columns += [name for name, _, _ in hierarchy._code_columns(graph, table)]
        for column in dict.fromkeys(columns):
            if hierarchy.level_id(table, column) in taken:
                continue
            if hierarchy.level_refusal(graph, record, table, column, path, lines=lines,
                                       primary=primary):
                continue
            level = hierarchy.level_for(graph, record, table, column, path, bridge=through)
            if level is not None and level.distinct <= hierarchy.MAX_LEVEL_DISTINCT:
                found.append(level)
    found.sort(key=lambda lv: (-lv.confidence, lv.distinct, lv.id))
    return [{"id": lv.id, "table": lv.table, "column": lv.column, "kind": lv.kind,
             "distinct": lv.distinct, "label_column": lv.label_column,
             "confidence": lv.confidence, "reason": lv.reason}
            for lv in found[:ALTERNATIVES_SHOWN]]


def swap(held: SourcePlan, role: str, level_id: str, table: str, column: str, *,
         graph: KeyGraph) -> SourcePlan:
    """Swap a level for another table's column, in one step whichever it is.
    A column that is not yet a level is swapped in by plan.swap_level. One
    the proposal already offers, unselected, takes the selected level's place
    instead: selected, put where the old one was, and the old one deselected
    -- the same three edits a person would make, each with the engine's own
    checks."""
    new_id = hierarchy.level_id(table, column)
    offered = {lv.id: lv for lv in held.levels(role)} if role in ROLES else {}
    if new_id == level_id or new_id not in offered:
        return plan.swap_level(held, role, level_id, table, column, graph=graph)
    old = plan._level(held, role, level_id)
    if not old.selected:
        raise ValueError(f"{level_id} is not selected: select {new_id} instead")
    if offered[new_id].selected:
        raise ValueError(f"{new_id} is selected already: deselect {level_id} instead")
    order = [lv.id for lv in sorted(held.levels(role), key=lambda lv: lv.order) if lv.selected]
    order[order.index(level_id)] = new_id
    held = plan.select_level(held, role, new_id, True, graph=graph)
    held = plan.reorder_levels(held, role, order, graph=graph)
    return plan.select_level(held, role, level_id, False, graph=graph)


def _view(read: landscape.Landscape, held: SourcePlan, graph: KeyGraph | None) -> dict[str, Any]:
    stamp = plan.landscape_stamp(read)
    stale = bool(stamp) and held.read_at != stamp
    return {
        "source": read.name,
        "stale": stale,
        "stale_reason": STALE_SAID if stale else None,
        "plan": held.to_dict(),
        "alternatives": {role: (alternatives(held, role, graph) if graph is not None and not stale
                                else []) for role in ROLES},
        "estimated": estimated(held),
        "grain": grain_view(held, graph) if graph is not None and not stale else {
            r: [] for r in ROLES},
        "primary_joins": primary_joins(held, graph) if graph is not None and not stale else [],
    }


def grain_view(held: SourcePlan, graph: KeyGraph) -> dict[str, list[dict[str, Any]]]:
    """Each record kind's link tables entered without dates
    (structure.unpicked_links): the levels that enter each, and whether the
    person accepted that its records repeat once per link row."""
    out: dict[str, list[dict[str, Any]]] = {}
    for role in ROLES:
        if not held.record_kinds.get(role):
            out[role] = []
            continue
        accepted = set(held.grain_accepted.get(role, []))
        out[role] = [{"table": table,
                      "levels": [lv.column for lv in held.levels(role) if lv.selected
                                 and any(j.to_table == table for j in lv.path)],
                      "accepted": table in accepted}
                     for table in structure_rules.unpicked_links(held, role, graph)]
    return out


def primary_joins(held: SourcePlan, graph: KeyGraph) -> list[dict[str, Any]]:
    """The joins from the child's record table to the primary's, the one the
    extract follows first (structure.primary_edges), each marked when the
    person named it. More than one, and none named, is for the person to
    choose."""
    stated = structure_rules.stated_to_primary(held)
    return [{"from_table": e.child_table, "from_columns": list(e.child_columns),
             "to_table": e.parent_table, "to_columns": list(e.parent_columns),
             "kind": e.kind,
             "named": e.kind == "stated" or (tuple(e.child_columns),
                                             tuple(e.parent_columns)) in stated}
            for e in structure_rules.primary_edges(held, graph)]


def proposal(name: str) -> dict[str, Any] | None:
    """The saved proposal as the screen shows it, with the alternatives for
    its levels; None when there is none. A proposal older than the landscape
    is handed back marked stale, without alternatives: it can be looked at,
    not edited."""
    folder = sources.folder_for(name)
    if not (folder / plan.PROPOSAL_FILE).exists():
        return None
    read = saved_landscape(name)
    held = plan.load(folder)
    if _stale(read, held):
        return _view(read, held, None)
    # Shown with its criteria and structure proposed again, as an edit
    # would save them: a proposal saved before they existed has them too.
    graph = plan.graph_for(read, held)
    return _view(read, plan.realign(held, graph), graph)


def edit(name: str, change: Callable[[SourcePlan, KeyGraph], SourcePlan]) -> dict[str, Any]:
    """Load the proposal, refuse it when older than the landscape, apply one
    edit with the key graph from the saved landscape (nothing is sent to the
    source), save it, and hand back the whole plan with its tables derived
    again. A refused edit (a ValueError, in the engine's words) saves nothing."""
    with _editing:
        read, folder, held = _held(name)
        _refuse_stale(read, held)
        graph = plan.graph_for(read, held)
        held = change(held, graph)
        # The graph again from the plan's joins, as the next load builds it
        # (a stated join may have replaced another), and every edit realigns
        # the criteria and structure, whichever it was.
        graph = plan.graph_for(read, held)
        held = plan.realign(held, graph)
        plan.save(held, folder)
        # Any edit drops an approval given to the plan as it was.
        _drop_approval(read.name, held)
        return _view(read, held, graph)


# --- phase 2: the plan's state, as the screen asks for it ------------------------

def _stale(read: landscape.Landscape, held: SourcePlan) -> bool:
    stamp = plan.landscape_stamp(read)
    return bool(stamp) and held.read_at != stamp


def _refuse_stale(read: landscape.Landscape, held: SourcePlan) -> None:
    try:
        plan.refuse_stale(held, read)
    except ValueError as exc:
        raise Conflict(STALE_SAID, "stale") from exc


def _held(name: str) -> tuple[landscape.Landscape, Path, SourcePlan]:
    """The saved landscape, the source's folder and its saved proposal;
    refused when either is missing."""
    read = saved_landscape(name)
    folder = sources.folder_for(name)
    try:
        held = plan.load(folder)
    except FileNotFoundError as exc:
        raise Conflict(f"{read.name} has no proposal yet: propose first.",
                       "no_proposal") from exc
    return read, folder, held


def _store(call: Callable[[], Any], done: str = "") -> Any:
    """A call into the store, a database error said by its kind and codes
    only (its own words can quote what it was given). `done` says what was
    saved already, when the call came after a save."""
    try:
        return call()
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise StoreFailed(f"{done}The store, where approvals are kept, could not be reached "
                          f"({error_said(exc)}). Check SDG_DENORM_* in .env.") from exc


def _drop_approval(name: str, held: SourcePlan) -> None:
    """Any change drops an approval given to the plan as it was. The change
    is saved already; a build still refuses the changed plan."""
    _store(lambda: approval.changed(name, plan.plan_hash(held)),
           "The change is saved, but its approval could not be dropped. ")


def estimated(held: SourcePlan) -> list[str]:
    """The criteria whose share was measured on a table's sample: an
    estimate, said so (criteria.measure notes each one)."""
    prefix = criteria_rules.SAMPLED + ", "
    sampled = {note[len(prefix):].split(": ", 1)[0] for note in held.notes
               if note.startswith(prefix)}
    return sorted(c.id for c in held.all_criteria()
                  if c.share_kept is not None and c.id in sampled)


def _realigned(name: str) -> tuple[landscape.Landscape, SourcePlan, bool]:
    """The saved plan with its criteria and structure proposed again (not
    saved), and whether it is older than the landscape (then as saved)."""
    read, _, held = _held(name)
    if _stale(read, held):
        return read, held, True
    return read, plan.realign(held, plan.graph_for(read, held)), False


def _measure_notes(held: SourcePlan) -> list[str]:
    return [n for n in held.notes
            if n.startswith((criteria_rules.NOT_MEASURED, criteria_rules.SAMPLED))]


def criteria_view(name: str) -> dict[str, Any]:
    """Each record kind's criteria: what each keeps, whether it is selected,
    the share measured (an estimate where a sample was read), and the notes
    of the last measure."""
    read, held, stale = _realigned(name)
    sampled = set(estimated(held))
    return {
        "source": read.name,
        "stale": stale,
        "criteria": {role: [c.to_dict() | {"estimated": c.id in sampled}
                            for c in held.criteria.get(role, [])] for role in ROLES},
        "notes": _measure_notes(held),
    }


def structure_view(name: str) -> dict[str, Any]:
    """The denorm tables, every column with what it serves and its privacy
    class, and the pack's name."""
    read, held, stale = _realigned(name)
    graph = None if stale else plan.graph_for(read, held)
    return {"source": read.name, "stale": stale,
            "structure": [t.to_dict() for t in held.structure],
            # Link tables entered without dates, by role, and those whose
            # repeated records the person accepted (plan.accept_grain).
            "grain_links": {r: (structure_rules.unpicked_links(held, r, graph)
                                if graph is not None and held.record_kinds.get(r) else [])
                            for r in ROLES},
            "grain_accepted": {r: list(held.grain_accepted.get(r, [])) for r in ROLES},
            "pack": {"id": held.pack.get("id"), "name": held.pack.get("name")}}


MEASURE_WORDS = "Counting the rows each criterion keeps"


def _same_rule(a: criteria_rules.Criterion, b: criteria_rules.Criterion) -> bool:
    return (a.table, a.kind, a.columns, a.values, a.months, a.op) == (
        b.table, b.kind, b.columns, b.values, b.months, b.op)


def refuse_measuring(name: str) -> None:
    """What measuring would refuse, asked before its job starts: no proposal,
    or one older than the landscape."""
    read, _, held = _held(name)
    _refuse_stale(read, held)


def measure_criteria(name: str, registry: sources.SourceRegistry, stage: Stage) -> dict[str, Any]:
    """Measure the share of rows each criterion keeps (criteria.measure: one
    count each, read from the source), then save the shares into the plan
    as it is by then: a criterion changed while it was counted keeps no
    share. Any share saved drops an approval, as an edit does."""
    read, _, held = _held(name)
    _refuse_stale(read, held)
    graph = plan.graph_for(read, held)
    held = plan.realign(held, graph)
    measured = {c.id: c for c in held.all_criteria()}
    stage(0, 1, MEASURE_WORDS)
    checks = SourceChecks(registry)
    notes = criteria_rules.measure(list(measured.values()), graph, checks)
    with _editing:
        read, folder, now = _held(name)
        _refuse_stale(read, now)
        now = plan.realign(now, plan.graph_for(read, now))
        for criterion in now.all_criteria():
            was = measured.get(criterion.id)
            criterion.share_kept = (was.share_kept if was is not None
                                    and _same_rule(was, criterion) else None)
        now.status = "draft"
        now.notes = [n for n in now.notes if n not in _measure_notes(now)] + notes
        plan.save(now, folder)
        _drop_approval(read.name, now)
    stage(1, 1, "Done")
    return {"source": read.name,
            "measured": sum(1 for c in now.all_criteria() if c.share_kept is not None),
            "notes": notes + checks.notes()}


# --- phase 2: approval ----------------------------------------------------------

# A plan submitted or approved that is no longer the plan saved.
STATUS_CHANGED = "changed"


def _when(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value) if value else None


def approval_view(name: str) -> dict[str, Any]:
    """Where the plan stands: its status as the store holds it, checked
    against the plan saved now -- "approved" only while the saved plan has
    the hash that was approved, "changed" when a submitted or approved plan
    is no longer the one saved -- who submitted, approved and confirmed it
    and when, what is left to do before it can be submitted, and who must
    confirm an approval."""
    read, _, saved = _held(name)
    stale = _stale(read, saved)
    graph = None if stale else plan.graph_for(read, saved)
    current = saved if stale else plan.realign(SourcePlan.from_dict(saved.to_dict()), graph)
    digest = plan.plan_hash(saved)
    row = _store(lambda: approval.record(read.name)) or {}
    status = row.get("status") or "draft"
    if status != "draft" and row.get("plan_hash") != digest:
        status = STATUS_CHANGED
    named, unreadable = approval.second_approver()
    approved = status == "approved"
    return {
        "source": read.name,
        "stale": stale,
        "status": status,
        "plan_hash": digest,
        "submitted_by": row.get("submitted_by") if status != "draft" else None,
        "submitted_at": _when(row.get("submitted_at")) if status != "draft" else None,
        "approved_by": row.get("approved_by") if approved else None,
        "approved_at": _when(row.get("approved_at")) if approved else None,
        "confirmed_by": row.get("confirmed_by") if approved else None,
        # A build of an earlier plan is not this plan's.
        "built_at": (_when(row.get("built_at"))
                     if approved and row.get("built_hash")
                     and row.get("built_plan_hash") == digest else None),
        "problems": [STALE_SAID] if stale else approval.problems(current, graph),
        "second_approver": named,
        "approver_unreadable": unreadable,
        "pack": {"id": saved.pack.get("id"), "name": saved.pack.get("name")},
    }


def _mark(folder: Path, held: SourcePlan, status: str) -> None:
    """The saved plan's status, as the store now holds it; its content, and
    so its hash, is unchanged."""
    held.status = status
    plan.save(held, folder)


def submit(name: str) -> dict[str, Any]:
    """Submit the plan for review, as realigned and saved first so the file
    and the store hold the same plan. Refused while it has problems."""
    with _editing:
        read, folder, held = _held(name)
        _refuse_stale(read, held)
        graph = plan.graph_for(read, held)
        held = plan.realign(held, graph)
        found = approval.problems(held, graph)
        if found:
            raise Conflict("The plan cannot be submitted yet: " + " ".join(found), "not_ready")
        plan.save(held, folder)
        _drop_approval(read.name, held)
        done = _store(lambda: approval.submit(read.name, held, approval.who(), graph))
        _mark(folder, held, done["status"])
    return approval_view(name)


def approve(name: str, plan_hash: str, typed: str | None) -> dict[str, Any]:
    """Approve the plan waiting for review, as the person looked at it:
    `plan_hash` is the hash the screen showed, refused when the saved plan
    is no longer that one. `typed` is the second approver's name, typed to
    confirm, when one is set."""
    with _editing:
        read, folder, held = _held(name)
        _refuse_stale(read, held)
        digest = plan.plan_hash(held)
        if plan_hash != digest:
            raise Conflict("The plan has changed since this page showed it. Look at it again, "
                           "then approve.", "changed")
        approved_by, confirmed_by = approval.approver(typed)
        done = _store(lambda: approval.approve(read.name, digest, approved_by, confirmed_by))
        _mark(folder, held, done["status"])
    return approval_view(name)


def withdraw(name: str) -> dict[str, Any]:
    """Take a submitted or approved plan back to draft."""
    with _editing:
        read, folder, held = _held(name)
        done = _store(lambda: approval.withdraw(read.name))
        _mark(folder, held, done["status"])
    return approval_view(name)


# --- phase 2: build ---------------------------------------------------------------

BUILD_WORDS = "Writing the pack and starting SDM on it"
# A built file is shown or downloaded only up to this size.
FILE_MAX_BYTES = 5_000_000


def _packs_folder() -> Path:
    try:
        return builder.packs_folder()
    except builder.BuildRefused as exc:
        raise Conflict(str(exc), "packs_dir") from exc


def _pack_id(held: SourcePlan) -> str:
    try:
        return builder.check_pack_id((held.pack or {}).get("id"))
    except builder.BuildRefused as exc:
        raise Conflict(f"{exc} Name it under Approval.", "pack_id") from exc


def refuse_building(name: str) -> None:
    """What a build would refuse, asked before its job starts so a refusal
    is an answer: the packs folder, the pack's id, and an approval that
    still holds for the plan saved now."""
    _packs_folder()
    read, _, held = _held(name)
    _refuse_stale(read, held)
    _pack_id(held)
    _store(lambda: approval.approved_plan(read.name))


def build_pack(name: str, stage: Stage) -> dict[str, Any]:
    """Build the approved plan's pack (builder.build). A pack SDM would not
    start on is left aside, its problems handed back; one that starts is put
    in place."""
    source = sources.plain_name(name)
    stage(0, 1, BUILD_WORDS)
    done = _store(lambda: builder.build(source, by=approval.who()))
    stage(1, 1, "Done")
    placed = not done.problems
    return {"source": source, "pack_id": done.pack_id, "placed": placed,
            "folder": str(done.folder) if placed else None,
            "packs_dir": str(done.folder.parent) if placed else None,
            "files": sorted(done.files), "problems": list(done.problems)}


def _built_folder(name: str) -> tuple[str, Path]:
    """The source's name and the folder its plan builds; never a link to
    somewhere else."""
    read, _, held = _held(name)
    folder = _packs_folder() / _pack_id(held)
    if builder._linked(folder):
        raise Conflict(f"{folder} is a link to somewhere else: its files are not shown.",
                       "packs_dir")
    return read.name, folder


def built_files(name: str) -> dict[str, Any]:
    """The files a build writes, each with whether it is there."""
    source, folder = _built_folder(name)
    files = []
    for listed in builder.built_files(source):
        path = folder / listed
        there = path.is_file() and not path.is_symlink()
        files.append({"name": listed, "built": there,
                      "bytes": path.stat().st_size if there else None})
    return {"source": source, "pack_id": folder.name, "folder": str(folder),
            "packs_dir": str(folder.parent), "files": files}


def built_file(name: str, file: str) -> tuple[str, str]:
    """One built file's name and text. The name must be one a build writes,
    exactly (builder.built_files); the file is read by the list's own
    spelling, inside the pack's folder, never by a path the request gives."""
    source, folder = _built_folder(name)
    allowed = builder.built_files(source)
    if file not in allowed:
        raise Conflict("That is not a file a build writes.", "unknown_file", 404)
    listed = allowed[allowed.index(file)]
    path = folder / listed
    if not path.is_file():
        raise Conflict(f"{listed} is not built yet: build the pack first.", "not_built", 404)
    if path.is_symlink() or not builder._inside(path.resolve(), folder.resolve()):
        raise Conflict(f"{listed} is a link to somewhere else: it is not shown.", "not_built",
                       404)
    if path.stat().st_size > FILE_MAX_BYTES:
        raise Conflict(f"{listed} is larger than a page can show: open it in the pack folder.",
                       "too_large")
    try:
        return listed, path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Conflict(f"{listed} is not UTF-8 text, as a build writes it: it was changed by "
                       f"hand. Build the pack again.", "not_text") from exc
