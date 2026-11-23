"""`sdg accelerate`: read a new source and draft its setup.

    sdg accelerate landscape BANK_DEMO
    sdg accelerate propose BANK_DEMO
    sdg accelerate show BANK_DEMO
    sdg accelerate select|deselect BANK_DEMO child LEVEL_ID
    sdg accelerate reorder BANK_DEMO child LEVEL_ID LEVEL_ID ...
    sdg accelerate swap BANK_DEMO child LEVEL_ID TABLE COLUMN
    sdg accelerate name BANK_DEMO primary customer customers
    sdg accelerate choose-record BANK_DEMO child TABLE
    sdg accelerate criteria BANK_DEMO [--measure]
    sdg accelerate criterion BANK_DEMO CRITERION_ID [--on|--off] [--value V ...] [--months N]
    sdg accelerate structure BANK_DEMO
    sdg accelerate privacy BANK_DEMO TABLE COLUMN kept|withheld|surrogate
    sdg accelerate extra BANK_DEMO child TABLE COLUMN
    sdg accelerate pack-name BANK_DEMO PACK_ID "Pack name"
    sdg accelerate submit BANK_DEMO
    sdg accelerate approve BANK_DEMO [--approver "Second approver"]
    sdg accelerate withdraw BANK_DEMO
    sdg accelerate build BANK_DEMO [--packs-dir FOLDER]
    sdg accelerate files BANK_DEMO [FILE]

A source is a name; its connection comes from SDG_SOURCE_<NAME>_* in the
.env file, each setting falling back to the configured server's
(sdg/accelerator/sources.py). Everything is written under
work/accelerator/<NAME>/, and the source is only ever read. propose reads
it once more, for composite keys and nestings the profile left open, unless
--offline; every edit after it works from the saved figures alone, and
proposes the criteria and the denorm structure again, keeping the choices
made in them. criteria --measure reads the source once more: one count per
criterion, over a fixed sample of a large table.

submit, approve and withdraw keep the plan's approval in the store
(sdm.source_plans, sdg/accelerator/approval.py); every edit drops it.
build writes the approved plan's pack under SDG_PACKS_DIR (outside SDM),
checked by starting SDM on it in a process of its own; files lists what it
wrote, or prints one file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import typer
from rich.markup import escape
from rich.table import Table

if TYPE_CHECKING:
    from sdg.accelerator.plan import SourcePlan

accelerate = typer.Typer(
    add_completion=False,
    help="Read a new source and draft its setup: landscape, then propose.",
    no_args_is_help=True,
)


@accelerate.command()
def landscape(
    name: str = typer.Argument(..., help="The source's name, as in SDG_SOURCE_<NAME>_*."),
    schemas: list[str] = typer.Option(None, "--schemas", help="read only these schemas"),
    minutes: float = typer.Option(
        None, "--minutes",
        help="limit on the profile's pairwise measures; SDG_PROFILE_MAX_MINUTES by default"),
    allow_write_login: bool = typer.Option(
        False, "--allow-write-login",
        help="go on although the login can write to the source (it is still only read)"),
) -> None:
    """Read the source's structure and keys, and profile the tables that matter."""
    from sdg.accelerator import landscape as reading
    from sdg.accelerator.source_read import NotARead
    from sdg.accelerator.sources import SourceError
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    try:
        read = reading.read(name, include_schemas=schemas or None, max_minutes=minutes,
                            allow_write_login=allow_write_login)
        folder = reading.save(read)
    except (SourceError, NotARead, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _fail(_unreadable(name, exc)) from exc

    summary = read.summary
    counts = Table(title=f"{escape(read.name)} landscape")
    counts.add_column("")
    counts.add_column("", justify="right")
    for label, value in (
        ("tables", summary["tables"]),
        ("columns", summary["columns"]),
        ("declared keys", summary["declared_keys"]),
        ("inferred keys", summary["inferred_keys"]),
        ("shortlisted", len(summary["shortlisted"])),
        ("analyses left out", len(summary["skipped"])),
        ("code tables", len(summary["code_tables"])),
        ("date-pair tables", len(summary["date_pair_tables"])),
        ("person-like tables", len(summary["person_like_tables"])),
        ("login can write", "yes" if (summary.get("login") or {}).get("can_write") else "no"),
        ("seconds", summary["seconds"]),
    ):
        counts.add_row(label, f"{value:,}" if isinstance(value, int) else str(value))
    console.print(counts)

    if summary["hubs"]:
        hubs = Table(title="Most referenced tables")
        hubs.add_column("table", overflow="fold")
        hubs.add_column("referenced by", justify="right")
        hubs.add_column("rows", justify="right")
        for hub in summary["hubs"]:
            hubs.add_row(escape(hub["table"]), str(hub["referenced_by"]), f"{hub['rows']:,}")
        console.print(hubs)

    for skipped in summary["skipped"][:5]:
        console.print(f"  [yellow]left out[/yellow] {escape(skipped['table'])} "
                      f"{escape(skipped['analysis'])}: {escape(skipped['reason'])}")
    if len(summary["skipped"]) > 5:
        console.print(f"  [dim]and {len(summary['skipped']) - 5} more, in landscape.json[/dim]")
    console.print(f"wrote {escape(str(folder))}")


ROLE_HELP = "primary or child"


def _unreadable(name: str, exc: BaseException) -> str:
    """A database error, said by its kind and codes only: the driver's own
    words can quote a value from the source (source_read.error_said)."""
    from sdg.accelerator.workbench import unreadable

    return unreadable(name, exc)


def _fail(message: str) -> typer.Exit:
    """Print the message in red and return the exit to raise."""
    from sdg.cli.main import console

    console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=1)


def _saved(name: str) -> tuple[Any, Path]:
    """The source's saved landscape and its folder; refused when either is missing."""
    from sdg.accelerator import landscape as reading
    from sdg.accelerator import sources

    try:
        return reading.load(name), sources.folder_for(name)
    except (sources.SourceError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc


def _edit(name: str, change: Callable[["SourcePlan", Any], "SourcePlan"],
          shown: Callable[["SourcePlan"], None] | None = None) -> None:
    """Load the plan, apply one edit (given the plan and the key graph, read
    from the saved landscape only), save it, show it (with `shown`, or the
    record kinds and levels)."""
    from sdg.accelerator import plan as planning
    from sdg.cli.main import console

    read, folder = _saved(name)
    try:
        held = planning.load(folder)
        planning.refuse_stale(held, read)
        graph = planning.graph_for(read, held)
        # Every edit realigns the criteria and structure, whichever it was.
        held = planning.realign(change(held, graph), graph)
    except (ValueError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    path = planning.save(held, folder)
    _changed(read.name, held)
    (shown or _show)(held)
    console.print(f"wrote {escape(str(path))}")


def _changed(name: str, held: "SourcePlan") -> None:
    """Any edit drops an approval given to the plan as it was (approval.changed).
    The edit is saved already; a store that cannot be reached is said, and a
    build still refuses the changed plan (approval.approved_plan)."""
    from sdg.accelerator import approval
    from sdg.accelerator import plan as planning
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error, plain

    try:
        approval.changed(name, planning.plan_hash(held))
    except ProtectedDatabase as exc:
        raise _fail(f"The edit is saved, but its approval could not be dropped: {exc}") from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _fail(f"The edit is saved, but its approval could not be dropped in the store: "
                    f"{plain(exc)}") from exc


def _show(held: "SourcePlan") -> None:
    """The plan as a person reads it: record kinds and their candidates, each
    role's levels in order, the tables they need. Names and counts only."""
    from sdg.cli.main import console

    kinds = Table(title=f"{held.source} record kinds ({held.status})")
    for column in ("role", "table", "lines", "name", "candidates"):
        kinds.add_column(column, overflow="fold")
    for role in ("primary", "child"):
        kind = held.record_kinds.get(role)
        others = ", ".join(f"{c.table} {c.score:.2f}" for c in held.candidates.get(role, [])[:3])
        if not kind:
            kinds.add_row(role, "(none)", "", "", escape(others))
            continue
        name = kind.get("name") or {}
        named = f"{name.get('one')} / {name.get('many')}" if name.get("one") else "(unnamed)"
        kinds.add_row(role, escape(kind["table"]), escape(kind.get("lines_table") or ""),
                      escape(named), escape(others))
    console.print(kinds)

    for role in ("primary", "child"):
        levels = held.levels(role)
        if not held.record_kinds.get(role) or not levels:
            continue
        shown = Table(title=f"{role} levels")
        for column, justify in (("#", "right"), ("on", "left"), ("kind", "left"),
                                ("level", "left"), ("label", "left"), ("values", "right"),
                                ("conf", "right"), ("why", "left")):
            shown.add_column(column, justify=justify, overflow="fold")
        for level in sorted(levels, key=lambda lv: lv.order):
            shown.add_row(str(level.order), "yes" if level.selected else "", level.kind,
                          escape(level.id), escape(level.label_column or ""),
                          f"{level.distinct:,}", f"{level.confidence:.2f}", escape(level.reason))
        console.print(shown)
        features = held.features(role)
        console.print(f"  {role} features: {sum(f.selected for f in features)} of "
                      f"{len(features)} selected")

    tables = Table(title="Source tables")
    tables.add_column("table", overflow="fold")
    tables.add_column("serves", overflow="fold")
    for table in held.tables:
        serves = sorted({f"{s['role']} {s['as']}" for s in table.serves})
        tables.add_row(escape(table.table), escape(", ".join(serves)))
    console.print(tables)
    for note in held.notes:
        console.print(f"  [yellow]note[/yellow] {escape(note)}")


@accelerate.command()
def propose(
    name: str = typer.Argument(..., help="The source's name, as in SDG_SOURCE_<NAME>_*."),
    primary: str = typer.Option(None, "--primary", help="the primary record's table"),
    child: str = typer.Option(None, "--child", help="the child record's table"),
    offline: bool = typer.Option(
        False, "--offline",
        help="propose from the saved landscape alone, sending nothing to the source"),
    force: bool = typer.Option(
        False, "--force",
        help="replace the saved proposal, and every choice made in it"),
    allow_write_login: bool = typer.Option(
        False, "--allow-write-login",
        help="go on although the login can write to the source (it is still only read)"),
    check_minutes: float = typer.Option(
        None, "--check-minutes",
        help="time limit on the checks sent to the source, all together; 5 by default"),
) -> None:
    """Propose record kinds, levels and the tables they need, from the landscape."""
    from sdg.accelerator import plan as planning
    from sdg.accelerator import sources
    from sdg.accelerator.source_read import NotARead
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    read, folder = _saved(name)
    if (folder / planning.PROPOSAL_FILE).exists() and not force:
        raise _fail(f"{folder / planning.PROPOSAL_FILE} is there already, with the choices made "
                    f"in it. `sdg accelerate show {name}` shows it; pass --force to replace it "
                    f"with a new proposal (its selections, names and stated joins are lost).")
    try:
        registry = None if offline else sources.registry_for(
            name, allow_write_login=allow_write_login)
        held = planning.propose(read, primary=primary, child=child, registry=registry,
                                check_seconds=None if check_minutes is None
                                else check_minutes * 60)
    except (ValueError, sources.SourceError, NotARead, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _fail(_unreadable(name, exc)) from exc
    path = planning.save(held, folder)
    _show(held)
    console.print(f"wrote {escape(str(path))}")


@accelerate.command()
def show(name: str = typer.Argument(..., help="The source's name.")) -> None:
    """Show the source's plan as last saved."""
    from sdg.accelerator import plan as planning
    from sdg.accelerator import sources
    from sdg.cli.main import console

    read, folder = _saved(name)
    try:
        held = planning.load(folder)
        planning.refuse_stale(held, read)
    except (sources.SourceError, ValueError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    _show(held)
    console.print(f"from {escape(str(folder / planning.PROPOSAL_FILE))}")


@accelerate.command()
def select(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    level_id: str = typer.Argument(..., help="the level's id, as show lists it"),
) -> None:
    """Select a level; the tables it needs join the plan."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.select_level(held, role, level_id, True,
                                                          graph=graph))


@accelerate.command()
def deselect(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    level_id: str = typer.Argument(..., help="the level's id, as show lists it"),
) -> None:
    """Deselect a level; a table leaves the plan when nothing else needs it."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.select_level(held, role, level_id, False,
                                                          graph=graph))


@accelerate.command()
def reorder(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    ids: list[str] = typer.Argument(..., help="selected level ids, in the order wanted"),
) -> None:
    """Put selected levels in the order given; the others follow."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.reorder_levels(held, role, ids, graph=graph))


@accelerate.command()
def swap(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    level_id: str = typer.Argument(..., help="the level to replace"),
    table: str = typer.Argument(..., help="the new level's table"),
    column: str = typer.Argument(..., help="the new level's column"),
) -> None:
    """Replace a level by another table's column, reached by its own joins."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.swap_level(held, role, level_id, table, column,
                                                        graph=graph))


@accelerate.command("name")
def name_kind(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    one: str = typer.Argument(..., help="what one record is called"),
    many: str = typer.Argument(..., help="what many are called"),
) -> None:
    """Name a record kind, one and many."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.name_record(held, role, one, many))


@accelerate.command("join")
def state_join(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    from_table: str = typer.Argument(..., help="the table the join leaves (the child's side)"),
    from_columns: str = typer.Argument(..., help="its columns, comma-separated"),
    to_table: str = typer.Argument(..., help="the table the join reaches (the key's side)"),
    to_columns: str = typer.Argument(..., help="its columns, comma-separated, in order"),
) -> None:
    """State a join inference did not find, or the one join the child follows
    to the primary where several link them."""
    from sdg.accelerator import plan as planning
    from sdg.accelerator.hierarchy import Join

    join = Join(from_table, [c.strip() for c in from_columns.split(",") if c.strip()],
                to_table, [c.strip() for c in to_columns.split(",") if c.strip()], "stated", None)
    _edit(name, lambda held, graph: planning.state_join(held, role, join, graph=graph))


@accelerate.command("choose-record")
def choose_record(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    table: str = typer.Argument(..., help="the record's table"),
) -> None:
    """Choose the primary or child record's table; its levels are proposed again."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.choose_record(held, role, table, graph=graph))


# --- phase 2: criteria, structure, pack ---------------------------------------

def _show_criteria(held: "SourcePlan") -> None:
    """Each role's criteria: selected or not, what they keep, the share
    measured, and why."""
    from sdg.cli.main import console

    shown = Table(title=f"{held.source} criteria ({held.status})")
    for column, justify in (("on", "left"), ("id", "left"), ("keeps", "left"),
                            ("share kept", "right"), ("why", "left")):
        shown.add_column(column, justify=justify, overflow="fold")
    for criterion in held.all_criteria():
        if criterion.kind == "in_force":
            keeps = f"in force today ({' to '.join(criterion.columns)})"
        elif criterion.kind == "window":
            keeps = f"last {criterion.months} months of {criterion.columns[0]}"
        else:
            listed = ", ".join(criterion.values) or "(no values yet)"
            keeps = f"{criterion.columns[0]} {criterion.op.replace('_', ' ')} {listed}"
        share = "" if criterion.share_kept is None else f"{criterion.share_kept:.1%}"
        shown.add_row("yes" if criterion.selected else "", escape(criterion.id), escape(keeps),
                      share, escape(criterion.reason))
    console.print(shown)
    if not held.all_criteria():
        console.print("  no criteria proposed: the extract keeps every row")


def _show_structure(held: "SourcePlan") -> None:
    """Each denorm table: its columns, what each serves, its privacy class
    and whether it is locked; the bridges picked; the pack's name."""
    from sdg.cli.main import console

    for denorm in held.structure:
        shown = Table(title=f"{denorm.name} ({denorm.role}, one row per {denorm.grain})")
        for column in ("column", "from", "type", "serves", "privacy", "locked", "why"):
            shown.add_column(column, overflow="fold")
        for column in denorm.columns:
            privacy = column.privacy + ("" if column.privacy == column.proposed
                                        else f" (proposed {column.proposed})")
            shown.add_row(escape(column.name),
                          escape(f"{column.source_table}.{column.source_column}"),
                          escape(column.sql_type), column.serves, privacy,
                          "yes" if column.locked else "", escape(column.reason))
        console.print(shown)
        for pick in denorm.pick:
            console.print(f"  picks the row of {escape(pick['table'])} in force today with the "
                          f"latest {escape(pick['start'])}")
        for table in held.grain_accepted.get(denorm.role, []):
            console.print(f"  repeats each record once per row of {escape(table)}, as accepted")
    pack = held.pack or {}
    console.print(f"  pack: {escape(pack.get('id') or '(not named)')}"
                  + (f" ({escape(pack['name'])})" if pack.get("name") else ""))


def _realigned(name: str) -> tuple["SourcePlan", Any, Path, Any]:
    """The saved plan with its criteria and structure proposed again (a
    version 1 proposal gets them here), the key graph, the folder and the
    landscape; refused when stale or missing."""
    from sdg.accelerator import plan as planning

    read, folder = _saved(name)
    try:
        held = planning.load(folder)
        planning.refuse_stale(held, read)
        graph = planning.graph_for(read, held)
        return planning.realign(held, graph), graph, folder, read
    except (ValueError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc


@accelerate.command()
def criteria(
    name: str = typer.Argument(..., help="The source's name."),
    measure: bool = typer.Option(
        False, "--measure",
        help="read the source for the share of rows each criterion keeps (one count each)"),
    allow_write_login: bool = typer.Option(
        False, "--allow-write-login",
        help="go on although the login can write to the source (it is still only read)"),
    check_minutes: float = typer.Option(
        None, "--check-minutes",
        help="time limit on the counts sent to the source, all together; 5 by default"),
) -> None:
    """Show the extraction criteria; with --measure, the share each keeps."""
    from sdg.accelerator import criteria as rules
    from sdg.accelerator import plan as planning
    from sdg.accelerator import sources
    from sdg.accelerator.graph import SourceChecks
    from sdg.accelerator.source_read import NotARead
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    held, graph, folder, _ = _realigned(name)
    if not measure:
        _show_criteria(held)
        return
    if check_minutes is not None and check_minutes <= 0:
        raise _fail("the time limit on the counts must be above zero")
    try:
        registry = sources.registry_for(name, allow_write_login=allow_write_login)
        checks = SourceChecks(registry)
        if check_minutes is not None:
            checks.seconds = check_minutes * 60
        notes = rules.measure(held.all_criteria(), graph, checks)
    except (ValueError, sources.SourceError, NotARead, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _fail(_unreadable(name, exc)) from exc
    # The shares are part of the plan: an approval given before them no longer holds.
    held.status = "draft"
    held.notes = [n for n in held.notes
                  if not n.startswith((rules.NOT_MEASURED, rules.SAMPLED))]
    held.notes += notes
    path = planning.save(held, folder)
    _changed(name, held)
    _show_criteria(held)
    for note in notes + checks.notes():
        console.print(f"  [yellow]note[/yellow] {escape(note)}")
    console.print(f"wrote {escape(str(path))}")


@accelerate.command()
def criterion(
    name: str = typer.Argument(..., help="The source's name."),
    criterion_id: str = typer.Argument(..., help="the criterion's id, as criteria lists it"),
    on: bool = typer.Option(None, "--on/--off", help="select or deselect it"),
    values: list[str] = typer.Option(
        None, "--value", help="a value to keep (a values criterion); repeat for several"),
    op: str = typer.Option(None, "--op", help="in or not_in (a values criterion)"),
    months: int = typer.Option(None, "--months", help="the window's months"),
) -> None:
    """Select, deselect or set a criterion: its values, op or months."""
    from sdg.accelerator import plan as planning

    if on is None and not values and op is None and months is None:
        raise _fail("nothing to change: give --on or --off, --value, --op or --months")

    def change(held: "SourcePlan", graph: Any) -> "SourcePlan":
        if values or op is not None or months is not None:
            held = planning.set_criterion(held, criterion_id, values=values or None,
                                          months=months, op=op, graph=graph)
        if on is not None:
            held = planning.select_criterion(held, criterion_id, on, graph=graph)
        return held

    _edit(name, change, _show_criteria)


@accelerate.command()
def structure(name: str = typer.Argument(..., help="The source's name.")) -> None:
    """Show the denorm tables: columns, privacy classes, bridges picked."""
    held, _, _, _ = _realigned(name)
    _show_structure(held)


@accelerate.command()
def privacy(
    name: str = typer.Argument(..., help="The source's name."),
    table: str = typer.Argument(..., help="the column's source table"),
    column: str = typer.Argument(..., help="the column, as the source names it"),
    kind: str = typer.Argument(..., help="kept, withheld or surrogate"),
    confirm_not_personal: bool = typer.Option(
        False, "--confirm-not-personal",
        help="keep a key proposed as a surrogate: your word that it holds nothing personal"),
) -> None:
    """Set a structure column's privacy class (a locked column is never kept)."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.set_privacy(
        held, table, column, kind, graph=graph, confirm_not_personal=confirm_not_personal),
        _show_structure)


@accelerate.command()
def grain(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    table: str = typer.Argument(..., help="a link table entered without dates"),
    accept: bool = typer.Option(True, "--accept/--undo",
                                help="accept the grain change, or take it back"),
) -> None:
    """Accept that a record kind's table repeats each record once per row of a
    link table entered without dates; the extract refuses it until then."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.accept_grain(held, role, table, accept,
                                                          graph=graph),
          _show_structure)


@accelerate.command()
def extra(
    name: str = typer.Argument(..., help="The source's name."),
    role: str = typer.Argument(..., help=ROLE_HELP),
    table: str = typer.Argument(..., help="a table the record kind reads"),
    column: str = typer.Argument(..., help="the column to add"),
) -> None:
    """Add a column to a record kind's denorm table."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.add_extra_column(held, role, table, column,
                                                              graph=graph),
          _show_structure)


@accelerate.command("pack-name")
def pack_name(
    name: str = typer.Argument(..., help="The source's name."),
    pack_id: str = typer.Argument(..., help="the pack's id: lowercase, its folder's name"),
    words: str = typer.Argument(..., help="the pack's name, as a person reads it"),
) -> None:
    """Name the pack the plan builds."""
    from sdg.accelerator import plan as planning

    _edit(name, lambda held, graph: planning.name_pack(held, pack_id, words), _show_structure)



# --- phase 2: approval ---------------------------------------------------------

def _store_failed(exc: BaseException) -> typer.Exit:
    """A store error, said by its kind and codes only."""
    from sdg.discovery.errors import plain

    return _fail(f"The store could not be written: {plain(exc)}")


def _mark(folder: Path, held: "SourcePlan", status: str) -> None:
    """The saved plan's status, as the store now holds it (show prints it);
    the plan's content, and so its hash, is unchanged."""
    from sdg.accelerator import plan as planning

    held.status = status
    planning.save(held, folder)


@accelerate.command()
def submit(name: str = typer.Argument(..., help="The source's name.")) -> None:
    """Submit the plan for review; refused while it has problems."""
    from sdg.accelerator import approval
    from sdg.accelerator import plan as planning
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    held, graph, folder, read = _realigned(name)
    for problem in approval.problems(held, graph):
        console.print(f"  [yellow]to do[/yellow] {escape(problem)}")
    # The plan as realigned is the plan submitted: saved first, so the file
    # and the store hold the same plan.
    planning.save(held, folder)
    _changed(read.name, held)
    try:
        done = approval.submit(read.name, held, approval.who(), graph)
    except (ValueError, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _store_failed(exc) from exc
    _mark(folder, held, done["status"])
    console.print(f"{escape(read.name)}'s plan is waiting for review "
                  f"(plan {done['plan_hash'][:12]}).")


@accelerate.command()
def approve(
    name: str = typer.Argument(..., help="The source's name."),
    approver: str = typer.Option(
        None, "--approver",
        help="the second approver's name, typed to confirm, when one is set"),
) -> None:
    """Approve the plan waiting for review, as it is saved now."""
    from sdg.accelerator import approval
    from sdg.accelerator import plan as planning
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    read, folder = _saved(name)
    try:
        held = planning.load(folder)
        planning.refuse_stale(held, read)
        approved_by, confirmed_by = approval.approver(approver)
        done = approval.approve(read.name, planning.plan_hash(held), approved_by, confirmed_by)
    except (ValueError, PermissionError, FileNotFoundError, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _store_failed(exc) from exc
    _mark(folder, held, done["status"])
    console.print(f"{escape(read.name)}'s plan is approved by {escape(done['approved_by'])}, "
                  f"confirmed by {escape(done['confirmed_by'] or '')}.")


@accelerate.command()
def withdraw(name: str = typer.Argument(..., help="The source's name.")) -> None:
    """Take a submitted or approved plan back to draft."""
    from sdg.accelerator import approval
    from sdg.accelerator import plan as planning
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    read, folder = _saved(name)
    try:
        held = planning.load(folder)
        done = approval.withdraw(read.name)
    except (ValueError, FileNotFoundError, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _store_failed(exc) from exc
    _mark(folder, held, done["status"])
    console.print(f"{escape(read.name)}'s plan is a draft again.")


# --- phase 2: build ------------------------------------------------------------

@accelerate.command()
def build(
    name: str = typer.Argument(..., help="The source's name."),
    packs_dir: Path = typer.Option(
        None, "--packs-dir",
        help="the folder generated packs are kept in; SDG_PACKS_DIR by default"),
) -> None:
    """Build the approved plan's pack: its files, checked, put in place."""
    from sdg.accelerator import approval, builder
    from sdg.cli.main import console
    from sdg.config import ProtectedDatabase
    from sdg.discovery.errors import is_database_error

    try:
        done = builder.build(name, by=approval.who(), packs_dir=packs_dir)
    except (ValueError, PermissionError, FileNotFoundError, ProtectedDatabase) as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:
        if not is_database_error(exc):
            raise
        raise _store_failed(exc) from exc
    if done.problems:
        for problem in done.problems:
            console.print(f"  [red]problem[/red] {escape(problem)}")
        raise _fail(f"The pack was written to {done.folder} but SDM would not start on it, so "
                    f"it was not put in place.")
    for file, digest in sorted(done.files.items()):
        console.print(f"  {escape(file)}  [dim]{digest[:12]}[/dim]")
    console.print(f"Built the {escape(done.pack_id)} pack in {escape(str(done.folder))}.")
    console.print(f"To use it, set SDG_PACKS_DIR={escape(str(done.folder.parent))} and "
                  f"SDG_PACK={escape(done.pack_id)} in .env, then restart SDM. README.txt in "
                  f"the folder says how to create its tables and fill them.")


@accelerate.command()
def files(
    name: str = typer.Argument(..., help="The source's name."),
    file: str = typer.Argument(None, help="one built file to print, as listed"),
    packs_dir: Path = typer.Option(
        None, "--packs-dir",
        help="the folder generated packs are kept in; SDG_PACKS_DIR by default"),
) -> None:
    """List the files the approved plan's build wrote, or print one."""
    from sdg.accelerator import builder, sources
    from sdg.cli.main import console

    try:
        source = sources.plain_name(name)
        if file:
            # Printed as it is: rich would read [brackets] in SQL as markup.
            typer.echo(builder.read_built(source, file, packs_dir), nl=False)
            return
        folder = builder.built_folder(source, packs_dir)
    except (ValueError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from exc
    for listed in builder.built_files(source):
        there = (folder / listed).is_file()
        console.print(f"  {escape(listed)}" + ("" if there else "  [yellow]not built[/yellow]"))
    console.print(f"in {escape(str(folder))}")
