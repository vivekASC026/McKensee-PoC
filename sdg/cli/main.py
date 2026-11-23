"""SDG command line. Subcommands are added as each build step lands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sdg.cli.accelerate import accelerate
from sdg.config import get_settings
from sdg.discovery.connections import ConnectionRegistry
from sdg.packs import held

app = typer.Typer(
    add_completion=False,
    help="Synthetic data generation tool (proof of concept).",
    no_args_is_help=True,
)
app.add_typer(accelerate, name="accelerate")
# A fixed width keeps table output readable when stdout is not a terminal,
# which is how the demo is usually captured.
console = Console(width=int(os.getenv("SDG_CONSOLE_WIDTH", "132")))


@app.callback()
def _main() -> None:
    """Keeps subcommand names visible even when only one command is registered."""


def work_dir() -> Path:
    path = get_settings().work_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_belongs_elsewhere() -> str | None:
    """The database work/ belongs to, when it is not the source in .env.

    The interface records the database being worked on in
    work/working-database.txt. Commands here read the source in .env and write
    into work/, so running one while the interface works on another database
    would put this source's model under that database's name.
    """
    import re

    marker = work_dir() / "working-database.txt"
    if not marker.exists():
        return None
    settings = get_settings()
    source = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{settings.mssql.server}__{settings.source_db}")
    current = marker.read_text(encoding="utf-8").strip()
    return current if current and current != source else None


def refuse_if_work_belongs_elsewhere() -> None:
    elsewhere = work_belongs_elsewhere()
    if elsewhere:
        elsewhere = elsewhere.split("__", 1)[-1]
        source = get_settings().source_db
        console.print(
            f"[red]work/ belongs to {elsewhere}, the database being worked on in "
            f"the interface.[/red] This would replace its files with {source}'s. "
            f"Choose {source} on Connect first, or read {elsewhere} again there instead."
        )
        raise typer.Exit(code=1)


def write_artifact(name: str, payload: object) -> Path:
    path = work_dir() / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def read_artifact(name: str) -> dict:
    path = work_dir() / name
    if not path.exists():
        raise typer.BadParameter(
            f"missing artifact {path}. Run the earlier step first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def read_work_file(loader, path: Path, step: str):
    """A work/ file read through its loader, or a message instead of a traceback.

    Old or hand-edited files fail validation or JSON parsing; both are
    ValueErrors (a pydantic ValidationError and a PlanError among them).
    """
    if not path.exists():
        raise typer.BadParameter(f"missing {path}. Run `sdg {step}` first.")
    try:
        return loader(path)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{path} could not be read: {exc}[/red]")
        console.print(f"Run `sdg {step}` again to make it afresh.")
        raise typer.Exit(code=1) from exc


def read_run_manifest(path: Path, missing: str) -> dict:
    """The run record, refused in words when it is missing or not a run record."""
    from sdg.writers.manifest import load_manifest

    if not path.exists():
        raise typer.BadParameter(f"missing {path}. {missing}")
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{path} could not be read: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    tables = manifest.get("tables") if isinstance(manifest, dict) else None
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        console.print(f"[red]{path} is not a run record: it has no list of tables.[/red]")
        raise typer.Exit(code=1)
    return manifest


@app.command()
def connect() -> None:
    """Check the configured connections and record them as an artifact."""
    registry = ConnectionRegistry()
    # The store is checked alongside the source: analysis reads it, so a run
    # that cannot reach it fails later rather than here.
    results = [registry.check(name) for name in ("source", "ref", "denorm")]

    table = Table(title="Connections")
    table.add_column("name")
    table.add_column("database")
    table.add_column("server")
    table.add_column("version")
    table.add_column("status")
    for result in results:
        table.add_row(
            result["connection"],
            result.get("db", "-"),
            result.get("server", "-"),
            result.get("version", "-"),
            "[green]ok[/green]" if result["ok"] else f"[red]{result.get('error', '')}[/red]",
        )
    console.print(table)

    path = write_artifact(
        "connections.json",
        {
            "connections": [registry.ref(n).as_dict() for n in ("source", "ref")],
            "checks": results,
        },
    )
    console.print(f"wrote {path}")
    if not all(r["ok"] for r in results):
        raise typer.Exit(code=1)


@app.command("work-check")
def work_check() -> None:
    """Fail if work/ holds another database's files than the source's."""
    refuse_if_work_belongs_elsewhere()


from sdg.config import LOCAL_SERVERS, customer_machine  # noqa: E402,F401  (used by the checks below)


@app.command("demo-check")
def demo_check() -> None:
    """Fail on a customer machine: the demo scripts build and reseed databases."""
    reason = customer_machine()
    if reason:
        console.print(
            f"[red]This machine is set up as a customer's ({reason} in .env).[/red]\n"
            "seed, bootstrap, first-run, upgrade and demo are for demo machines only:\n"
            "they create, reseed and re-extract databases. At a customer use only\n"
            "scripts\\customer\\setup_tables.cmd, scripts\\customer\\extract.cmd,\n"
            "scripts\\ui.cmd and scripts\\customer\\export_patterns.cmd (ENVIRONMENTS.md)."
        )
        raise typer.Exit(code=1)


# The source scripts/customer/extract.cmd reads (extract --configured): the
# one SDG_EXTRACT_SOURCE names, else the active pack's (pack.json
# extract.default_source).
EXTRACT_SOURCE = "SDG_EXTRACT_SOURCE"


@app.command()
def extract(
    source: str = typer.Argument(
        None, help="Which source to read. Omit to list what can be read."
    ),
    table: list[str] = typer.Option(
        None, "--table", help="Only these denorm tables. Repeatable."
    ),
    keep_going: bool = typer.Option(
        False, "--keep-going",
        help="If a table cannot be filled, keep its old rows, say why, and go on to the next.",
    ),
    configured: bool = typer.Option(
        False, "--configured",
        help=f"Read the source {EXTRACT_SOURCE} names in .env (the active pack's "
             "extract.default_source when it is not set).",
    ),
    allow_write_login: bool = typer.Option(
        False, "--allow-write-login",
        help="A pack the source accelerator built: go on although the source's login can "
             "write (the load only reads).",
    ),
) -> None:
    """Read a source into the analysis store.

    Pattern analysis reads the store, never a source directly, so this is what
    stands between a database and being able to analyze it. A refresh is
    complete: every row for this source is replaced. Other sources in the store
    are untouched.
    """
    from sdg.config import load_env
    from sdg.extract import extract as run_extract, sources

    if configured:
        if source:
            console.print("[red]Name a source or use --configured, not both.[/red]")
            raise typer.Exit(code=1)
        load_env()
        from sdg import packs

        source = (os.getenv(EXTRACT_SOURCE, "").strip()
                  or (packs.active().data.get("extract") or {}).get("default_source"))
        if not source:
            console.print(f"[red]No source to read.[/red] Set {EXTRACT_SOURCE} in .env to "
                          "the source's extract file name, or name the source.")
            raise typer.Exit(code=1)

    from sdg import packs as _packs

    running = _packs.active()
    # A pack a build wrote: its marker, or the kept columns every built pack
    # names (so removing the marker never opens the unchecked path).
    if source and (running.built_hash or (running.data.get("extract") or {}).get("kept")):
        # A pack the source accelerator built loads as the screen loads it:
        # the build checked against the store, every statement checked to
        # read, the source's own login, staged tables, earlier builds'
        # patterns archived.
        if table:
            console.print("[red]A built pack's tables load together;[/red] --table is not "
                          "taken.")
            raise typer.Exit(code=1)
        _extract_built(source, allow_write_login)
        return

    available = sources()
    if not source:
        if not available:
            console.print("[yellow]No extract definitions found.[/yellow]")
            raise typer.Exit(code=1)
        console.print("Sources that can be read:")
        for name in available:
            console.print(f"  {name}")
        return

    if source not in available:
        console.print(
            f"[red]No extract definition for {source}.[/red] "
            f"Found: {', '.join(available) or 'nothing'}"
        )
        raise typer.Exit(code=1)

    console.print(f"reading [cyan]{source}[/cyan] into the analysis store")

    # Rows this tool generated are not part of the landscape. The target is
    # usually the source, so without this a run followed by an extract measures
    # the next patterns partly from data the tool invented.
    from sdg.api.service import Service

    from sdg.extract import keys_from_manifest

    excluded = keys_from_manifest(Service().manifest)
    if excluded:
        console.print(
            "[dim]excluding the rows of the last run, which has not been "
            "cleaned up[/dim]"
        )

    def said(name: str, rows: int, done: bool, error: str = "") -> None:
        if error:
            console.print(f"  [red]{name}: FAILED[/red] -- {error} Its previous rows were kept.")
            return
        console.print(f"  {name}: {rows:,} rows{' -- done' if done else ' ...'}")

    result = run_extract(
        source, only=list(table) if table else None, exclude_keys=excluded,
        progress=said, stop_on_error=not keep_going,
    )

    out = Table(title=f"{source} extracted")
    out.add_column("table")
    out.add_column("rows", justify="right")
    out.add_column("generated, skipped", justify="right")
    out.add_column("seconds", justify="right")
    for row in result.tables:
        out.add_row(
            row.table, f"{row.rows:,}" if not row.error else "[red]failed[/red]",
            f"{row.skipped:,}" if row.skipped else "-",
            f"{row.seconds:.1f}",
        )
    console.print(out)
    failed = [row for row in result.tables if row.error]
    for row in failed:
        console.print(f"[red]{row.table}[/red]: {row.error} Its previous rows were kept.")
    if failed:
        raise typer.Exit(code=1)
    console.print(
        f"[green]{result.rows:,} rows in {result.seconds:.1f}s.[/green] "
        f"Patterns found before this read may no longer match; "
        f"the pattern screens report that as a stale pattern."
    )


def _extract_built(source: str, allow_write_login: bool) -> None:
    """Load a built pack's tables from its source (sdg.accelerator.loading)."""
    from sdg.accelerator import loading, sources as accel_sources
    from sdg.accelerator.source_read import NotARead
    from sdg.config import ProtectedDatabase

    def told(done: int, total: int, words: str) -> None:
        console.print(f"  {words}")

    console.print(f"loading [cyan]{source}[/cyan] into the analysis store (a built pack)")
    try:
        done = loading.load(source, told, lambda: False, allow_write_login=allow_write_login)
    except (loading.LoadRefused, accel_sources.SourceError, NotARead, ProtectedDatabase) as exc:
        console.print(f"[red]Not loaded.[/red] {exc}")
        raise typer.Exit(code=1)
    out = Table(title=f"{source} loaded")
    out.add_column("table")
    out.add_column("rows", justify="right")
    out.add_column("seconds", justify="right")
    for row in done["load"]["tables"]:
        out.add_row(row["table"], f"{row['rows']:,}" if not row["error"] else
                    "[red]not loaded[/red]", f"{row['seconds']:.1f}")
    console.print(out)
    for note in done["notes"]:
        console.print(f"[yellow]{note}[/yellow]")
    for check in done["checks"]:
        if not check["passed"]:
            console.print(f"[yellow]{check['table']}: {check['said']}[/yellow]")
    failed = [row for row in done["load"]["tables"] if row["error"]]
    for row in failed:
        console.print(f"[red]{row['table']}[/red]: {row['error']}")
    if failed:
        raise typer.Exit(code=1)


@app.command("customer-tables")
def customer_tables(
    indexes: bool = typer.Option(
        True, "--indexes/--no-indexes",
        help="Index the denorm tables for the reads pattern analysis makes (default on).",
    ),
) -> None:
    """Create every SDM table in the SDM platform database. Add-only.

    For a new deployment into an empty schema: the denorm tables
    (member_denorm, claims_denorm, and the provider, plan and claim-detail
    tables beside them), the tool's own tables, and the reference lists. Uses
    the store connection only (SDG_DENORM_*, SDG_DB_DENORM, SDG_DENORM_SCHEMA);
    FACETS is not touched and no database is created. Running it again
    creates only what is missing.
    """
    import sys

    import pyodbc

    from sdg.config import get_settings

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "seed"))
    from bootstrap import ensure_store

    settings = get_settings()
    schema = settings.denorm_schema
    from sdg.config import ProtectedDatabase, refuse_protected

    try:
        refuse_protected(settings.denorm_db, "create tables in")
    except ProtectedDatabase as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"SDM platform database [cyan]{settings.denorm_db}[/cyan] on "
                  f"[cyan]{settings.store.server}[/cyan], schema [cyan]{schema}[/cyan]")
    try:
        ensure_store(settings, indexes=indexes)
        # The tables the screens otherwise create on first use, made now so a
        # missing permission shows here and not in the middle of a demo.
        from sdg.analyzer.services import baseline, discovery_features, layout_service

        discovery_features.ensure_table()
        layout_service.ensure_table()
        layout_service.ensure_alias_table()
        layout_service._ensure_rule_measures()
        baseline.ensure_table()
        from sdg.analyzer.services import sample_gen_configs

        sample_gen_configs.ensure_table()
        from sdg.analyzer.services import report_fields

        report_fields.ensure_table()
        from sdg.analyzer.services import generation_requests

        generation_requests.ensure_tables()
        from sdg.extract.run import log_ddl

        with pyodbc.connect(settings.store.connection_string(settings.denorm_db)) as log:
            log.execute(log_ddl(schema))
            log.commit()
    except SystemExit as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        from sdg.discovery.errors import plain

        console.print(f"[red]Stopped:[/red] {plain(exc)}")
        raise typer.Exit(code=1)

    conn = pyodbc.connect(settings.store.connection_string(settings.denorm_db))
    try:
        rows = conn.cursor().execute(
            "SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p "
            "ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            "WHERE SCHEMA_NAME(t.schema_id) = ? GROUP BY t.name ORDER BY t.name",
            schema).fetchall()
    finally:
        conn.close()
    out = Table(title=f"{schema} tables ({len(rows)})")
    out.add_column("table")
    out.add_column("rows", justify="right")
    for name, count in rows:
        out.add_row(name, f"{int(count):,}")
    console.print(out)
    console.print("[green]Tables ready.[/green] Next: scripts\\customer\\extract.cmd")


@app.command()
def landscape(
    source: str = typer.Argument(
        None, help="Which extract to describe; the configured source by default.",
    ),
) -> None:
    """Describe what the store holds, per element.

    A profile is keyed by table and column, which makes it a fact about one
    database. This is the same measurements keyed by element, which makes them
    sayable about any target that carries the same elements -- including one
    that holds no rows at all.
    """
    from sdg.api.service import Service
    from sdg.landscape import derive, save
    from sdg.landscape.derive import unmeasurable

    service = Service()
    source = source or get_settings().source_db
    shape = derive(source=source, catalog=service.catalog, registry=service.registry)
    path = save(shape, service.work / f"landscape-{source}.json")

    out = Table(title=f"{source} landscape")
    out.add_column("")
    out.add_column("", justify="right")
    for key, value in shape.summary().items():
        out.add_row(key, f"{value:,}" if isinstance(value, int) else str(value))
    console.print(out)

    for relationship in shape.relationships:
        console.print(
            f"  [cyan]{relationship.child_element}[/cyan] per "
            f"[cyan]{relationship.parent_element}[/cyan]: "
            f"mean {relationship.mean:.2f}, {relationship.minimum}-{relationship.maximum}"
        )
    missed = unmeasurable()
    if missed:
        console.print(f"[yellow]{len(missed)} relationships could not be measured[/yellow]")
        for line in missed[:3]:
            console.print(f"  {line}")
    if shape.unmatched:
        console.print(
            f"[dim]{len(shape.unmatched)} store columns matched no element; "
            f"a target asking about those will be told nothing is known.[/dim]"
        )
    console.print(f"wrote {path}")


@app.command()
def discover(
    connection: str = typer.Option("source", help="which registered connection to read"),
    schema: list[str] = typer.Option(None, "--schema", help="limit to these schemas"),
    exclude: list[str] = typer.Option(None, "--exclude", help="skip these tables"),
    derivable: list[str] = typer.Option(
        None, "--derivable", help="declare a column as derived from others"
    ),
    no_infer: bool = typer.Option(False, "--no-infer", help="skip foreign-key inference"),
) -> None:
    """Read the structure of a database and classify every column."""
    from sdg.discovery.run import discover as run_discovery

    result = run_discovery(
        ConnectionRegistry(),
        connection,
        include_schemas=schema or None,
        exclude_tables=exclude or None,
        derivable_columns=derivable or (),
        infer_keys=not no_infer,
    )

    table = Table(title=f"Discovered {len(result.model.tables)} tables")
    table.add_column("table", overflow="fold")
    table.add_column("rows", justify="right")
    table.add_column("columns", justify="right")
    table.add_column("keys", overflow="fold")
    table.add_column("triggers", overflow="fold")
    for discovered in result.model.tables:
        declared = sum(1 for fk in discovered.foreign_keys if fk.kind == "declared")
        inferred = sum(1 for fk in discovered.foreign_keys if fk.kind == "inferred")
        keys = f"{declared} declared"
        if inferred:
            keys += f", [yellow]{inferred} inferred[/yellow]"
        table.add_row(
            discovered.name,
            f"{discovered.row_count:,}",
            str(len(discovered.columns)),
            keys,
            ", ".join(discovered.triggers) or "-",
        )
    console.print(table)

    if result.inferred:
        console.print("\n[bold]Inferred foreign keys[/bold]")
        for key in result.inferred:
            console.print(
                f"  {key.child_table}.{key.child_column} -> "
                f"{key.parent_table}.{key.parent_column}  "
                f"[dim]{key.coverage:.1%} of {key.distinct_values:,} distinct values[/dim]"
            )

    buckets = result.summary()["buckets"]
    console.print("\n[bold]Column buckets[/bold]  " + "  ".join(
        f"{name}={count}" for name, count in buckets.items()
    ))

    path = result.model.save(work_dir() / "physical-model.json")
    console.print(f"wrote {path}")


@app.command()
def profile(
    connection: str = typer.Option("source", help="which registered connection to read"),
    sample: int = typer.Option(100_000, help="cap on rows sampled per table"),
    table: list[str] = typer.Option(None, "--table", help="limit to these tables"),
    skip_pairwise: bool = typer.Option(
        False, "--skip-pairwise", help="skip dependency and ordering analysis"
    ),
) -> None:
    """Measure the data: distributions, formats, dependencies, and ratios."""
    from sdg.discovery.model import PhysicalModel
    from sdg.profiling.run import profile as run_profile

    model_path = work_dir() / "physical-model.json"
    if not model_path.exists():
        raise typer.BadParameter(f"missing {model_path}. Run `sdg discover` first.")
    model = PhysicalModel.load(model_path)

    report = run_profile(
        ConnectionRegistry(),
        model,
        connection,
        sample_cap=sample,
        tables=table or None,
        skip_pairwise=skip_pairwise,
    )

    summary = Table(title=f"Profiled {len(report.columns)} columns in {report.seconds:.1f}s")
    summary.add_column("table", overflow="fold")
    summary.add_column("seconds", justify="right")
    summary.add_column("columns", justify="right")
    summary.add_column("formats fitted", justify="right")
    summary.add_column("code columns", justify="right")
    for name, seconds in report.table_seconds.items():
        columns = [c for c in report.columns if c.table == name]
        summary.add_row(
            name,
            f"{seconds:.2f}",
            str(len(columns)),
            str(sum(1 for c in columns if c.fitted_format)),
            str(sum(1 for c in columns if c.looks_like_a_code())),
        )
    console.print(summary)

    if report.dependencies:
        console.print("\n[bold]Functional dependencies[/bold]")
        for dependency in report.dependencies:
            console.print(
                f"  {dependency.table}: {dependency.determinant} determines "
                f"{dependency.dependent} "
                f"[dim]across {dependency.distinct_determinant:,} distinct values[/dim]"
            )

    if report.orderings:
        console.print("\n[bold]Date orderings[/bold]")
        for ordering in report.orderings:
            console.print(
                f"  [{ordering.kind}] {ordering.earlier} <= {ordering.later}  "
                f"[dim]{ordering.share_in_order:.1%} of {ordering.rows_compared:,} rows, "
                f"gap {ordering.gap_min} to {ordering.gap_max} days[/dim]"
            )

    if report.ratios:
        console.print("\n[bold]Children per parent[/bold]")
        for ratio in report.ratios:
            console.print(
                f"  {ratio.child_table} per {ratio.parent_table}  "
                f"[cyan]{ratio.proposal['kind']}[/cyan]"
            )
            console.print(f"    [dim]{ratio.evidence}[/dim]")

    if report.skipped:
        console.print("\n[bold yellow]Skipped[/bold yellow]")
        for skipped in report.skipped:
            console.print(f"  {skipped.table} {skipped.analysis}: {skipped.reason}")

    path = report.save(work_dir() / "profile.json")
    console.print(f"\nwrote {path}")


@app.command(name="map")
def map_command(
    catalog: Path = typer.Option(None, "--catalog", help="catalog YAML to map against"),
) -> None:
    """Match every column to a catalog element, with the evidence behind it."""
    from sdg.catalog.loader import load_catalog
    from sdg.discovery.model import PhysicalModel
    from sdg.mapping.scorer import map_columns
    from sdg.profiling.model import ProfileReport

    model_path = work_dir() / "physical-model.json"
    if not model_path.exists():
        raise typer.BadParameter(f"missing {model_path}. Run `sdg discover` first.")
    model = PhysicalModel.load(model_path)

    profile_path = work_dir() / "profile.json"
    report = ProfileReport.load(profile_path) if profile_path.exists() else None
    if report is None:
        console.print(
            "[yellow]no profile found; mapping on names alone. "
            "Run `sdg profile` for format and check-digit evidence.[/yellow]"
        )

    loaded = load_catalog(catalog)
    store = map_columns(model, loaded, report)
    stats = store.statistics()

    table = Table(title=f"Mapped against {loaded.organization or 'the catalog'}")
    table.add_column("column", overflow="fold")
    table.add_column("element", overflow="fold")
    table.add_column("confidence")
    table.add_column("evidence", overflow="fold")
    for mapping in store.mappings:
        if mapping.bucket == "system":
            continue
        color = {"high": "green", "medium": "yellow", "low": "red"}.get(
            mapping.confidence or "", "dim"
        )
        table.add_row(
            mapping.qualified,
            mapping.element or "-",
            f"[{color}]{mapping.confidence or 'none'}[/{color}]",
            ", ".join(sorted({e["kind"] for e in mapping.evidence})) or "-",
        )
    console.print(table)

    console.print(
        f"\n[bold]Auto-map rate[/bold] "
        f"[green]{stats['auto_map_rate']:.1%}[/green] "
        f"({stats['high']} of {stats['mappable_columns']} mappable columns at high "
        f"confidence; {stats['system_columns']} system columns need no element)"
    )
    console.print(
        f"[bold]Review queue[/bold] "
        f"{stats['medium'] + stats['low'] + stats['unmapped']} columns "
        f"(medium {stats['medium']}, low {stats['low']}, unmapped {stats['unmapped']})"
    )
    console.print(
        "[dim]high-confidence evidence: "
        + ", ".join(f"{k} {v}" for k, v in stats["high_confidence_by_evidence"].items())
        + "[/dim]"
    )

    path = store.save(work_dir() / "mappings.json")
    console.print(f"wrote {path}")


def _load_review_inputs(catalog_path: Path | None, *, detect: bool = True):
    """Everything the proposer and the review page read. Shared by review and plan."""
    from sdg.catalog.loader import load_catalog
    from sdg.discovery.model import PhysicalModel
    from sdg.mapping.scorer import MappingStore
    from sdg.plan.detect import detect_sum_expressions
    from sdg.plan.proposer import propose
    from sdg.profiling.model import ProfileReport

    directory = work_dir()
    for name, step in (
        ("physical-model.json", "discover"),
        ("mappings.json", "map"),
    ):
        if not (directory / name).exists():
            raise typer.BadParameter(f"missing {directory / name}. Run `sdg {step}` first.")

    model = PhysicalModel.load(directory / "physical-model.json")
    mappings = MappingStore.load(directory / "mappings.json")
    profile_path = directory / "profile.json"
    report = ProfileReport.load(profile_path) if profile_path.exists() else None
    catalog = load_catalog(catalog_path)

    sums = detect_sum_expressions(ConnectionRegistry(), model) if detect else []
    proposals = propose(model, report, sums, mappings, catalog)
    return model, report, mappings, catalog, proposals, sums


@app.command()
def review(
    catalog: Path = typer.Option(None, "--catalog", help="catalog YAML to map against"),
    output: Path = typer.Option(None, "--output", help="where to write the HTML page"),
    open_page: bool = typer.Option(False, "--open", help="open the page in a browser"),
) -> None:
    """Propose a rule for every column and build the review page."""
    from sdg.review.page import build_context, write_page
    from sdg.review.queue import build_queue, decisions_template, save_decisions, summarize

    model, report, mappings, loaded, proposals, sums = _load_review_inputs(catalog)
    queue = build_queue(model, mappings, proposals)

    write_artifact("proposals.json", [p.as_dict() for p in proposals])
    write_artifact("review-queue.json", [q.as_dict() for q in queue])

    decisions_path = work_dir() / "review-decisions.json"
    if decisions_path.exists():
        console.print(f"[dim]keeping your edits in {decisions_path}[/dim]")
    else:
        save_decisions(decisions_template(queue), decisions_path)
        console.print(f"wrote {decisions_path} for you to edit")

    by_confidence: dict[str, int] = {}
    for proposal in proposals:
        by_confidence[proposal.confidence] = by_confidence.get(proposal.confidence, 0) + 1

    table = Table(title="Review queue")
    table.add_column("column", overflow="fold")
    table.add_column("bucket")
    table.add_column("why", overflow="fold")
    table.add_column("nearest element")
    table.add_column("proposed rule", overflow="fold")
    for item in queue:
        table.add_row(
            item.qualified, item.bucket, item.explain(),
            item.element or "-", item.generator or "-",
        )
    console.print(table if queue else "[green]Review queue is empty.[/green]")

    console.print(
        "\n[bold]Proposals[/bold]  "
        + "  ".join(f"{k} {v}" for k, v in sorted(by_confidence.items()))
    )
    if sums:
        console.print("[bold]Expressions found[/bold]")
        for found in sums:
            console.print(
                f"  {found.parent_table}.{found.parent_column} = "
                f"[cyan]{found.expression()}[/cyan] [dim]{found.detail()}[/dim]"
            )
    console.print(f"[bold]Queue size[/bold] {summarize(queue)['queued_columns']} columns")

    page_path = Path(output) if output else work_dir() / "review.html"
    context = build_context(
        model, report, mappings, proposals, queue, loaded,
        decisions_path=str(decisions_path),
    )
    write_page(context, page_path)
    console.print(f"wrote {page_path}")

    if open_page:
        import webbrowser

        webbrowser.open(page_path.resolve().as_uri())


@app.command()
def plan(
    catalog: Path = typer.Option(None, "--catalog", help="catalog YAML to resolve against"),
    seed: str = typer.Option("demo-seed-001", help="named seed for the run"),
    name: str = typer.Option(held("pack:plan_name"),
                             help="plan name, which keys the seed hash (the pack's)"),
    count: list[str] = typer.Option(
        None, "--count", help="root row count, as TABLE=N (repeatable)"
    ),
    reuse: list[str] = typer.Option(
        None, "--reuse", help="set a table to reuse rather than generate (repeatable)"
    ),
    exclude: list[str] = typer.Option(
        None, "--exclude", help="leave a table alone entirely (repeatable)"
    ),
    driving: list[str] = typer.Option(
        None, "--driving", help="override a driving parent, as CHILD=PARENT"
    ),
) -> None:
    """Resolve every column to one rule and compute the row counts."""
    from sdg.plan.build import BuildInputs, build_plan, default_reference_sets
    from sdg.plan.combinations import discover as discover_combinations
    from sdg.plan.hierarchy import HierarchyError, describe
    from sdg.plan.validator import blocking
    from sdg.review.queue import load_decisions

    model, report, mappings, loaded, proposals, _sums = _load_review_inputs(catalog)
    decisions = load_decisions(work_dir() / "review-decisions.json")

    modes = {name_: "reuse" for name_ in (reuse or [])}
    modes.update({name_: "exclude" for name_ in (exclude or [])})

    inputs = BuildInputs(
        model=model,
        catalog=loaded,
        mappings=mappings,
        proposals=proposals,
        report=report,
        decisions=decisions,
        modes=modes,
        root_counts=_parse_pairs(count, as_int=True),
        driving_parents=_parse_pairs(driving),
        seed=seed,
        name=name,
        catalog_path=str(catalog) if catalog else None,
    )
    inputs.reference_sets = default_reference_sets(ConnectionRegistry(), inputs)
    inputs.combinations = discover_combinations(ConnectionRegistry())

    try:
        built, graph = build_plan(inputs)
    except HierarchyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Effective counts")
    table.add_column("table", overflow="fold")
    table.add_column("mode")
    table.add_column("level", justify="right")
    table.add_column("driving parent", overflow="fold")
    table.add_column("ratio", justify="right")
    table.add_column("rows", justify="right")
    for plan_table in built.tables:
        node = graph.nodes.get(plan_table.name)
        table.add_row(
            plan_table.name,
            plan_table.mode,
            str(node.level) if node else "-",
            (node.driving_parent or "-") if node else "-",
            f"{node.ratio_mean:.2f}" if node and node.ratio_mean else "-",
            f"{node.effective_count:,}" if node else "-",
        )
    console.print(table)
    console.print(f"[bold]Total rows to generate[/bold] {graph.total_rows():,}")

    console.print("\n[bold]Hierarchy[/bold]")
    for line in describe(graph):
        console.print(f"  {line}")

    if built.meta.reference_sets:
        console.print("\n[bold]Reference sets[/bold]")
        for reference in inputs.reference_sets.values():
            console.print(f"  {reference.table}: [dim]{reference.describe()}[/dim]")

    provenance: dict[str, int] = {}
    for plan_table in built.tables:
        for field_ in plan_table.fields:
            provenance[field_.provenance] = provenance.get(field_.provenance, 0) + 1
    console.print(
        "\n[bold]Rule provenance[/bold]  "
        + "  ".join(f"{k} {v}" for k, v in sorted(provenance.items()))
    )

    stoppers = blocking(built.warnings)
    if built.warnings:
        console.print("\n[bold]Warnings[/bold]")
        for warning in built.warnings:
            color = "red" if warning.severity == "block" else "yellow"
            console.print(f"  [{color}]{warning.severity}[/{color}] {warning.code}: {warning.message}")

    path = built.save(work_dir() / "plan.json")
    console.print(f"\nwrote {path}")

    if stoppers:
        console.print(
            f"[red]{len(stoppers)} blocking warning"
            f"{'s' if len(stoppers) > 1 else ''}; generation will not run until they are cleared.[/red]"
        )
        raise typer.Exit(code=1)


def _parse_pairs(values: list[str] | None, *, as_int: bool = False) -> dict:
    out: dict = {}
    for item in values or []:
        if "=" not in item:
            raise typer.BadParameter(f"expected NAME=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = int(value) if as_int else value.strip()
    return out


@app.command()
def dryrun(
    rows: int = typer.Option(5, help="rows to show per table"),
) -> None:
    """Generate a few rows in memory and show them."""
    from sdg.engine.dryrun import dry_run, format_preview
    from sdg.engine.execute import ReferenceSets
    from sdg.plan.model import Plan as PlanModel
    from sdg.plan.refsets import load_for_plan

    refuse_if_work_belongs_elsewhere()
    loaded_plan = read_work_file(PlanModel.load, work_dir() / "plan.json", "plan")

    reference_sets = ReferenceSets()
    bindings = {
        table.name: {
            "connection": table.source.connection,
            "table": table.source.table,
            "schema": table.source.schema_name,
            "key_column": table.source.key_columns[0],
            "filter": table.source.filter,
            "load": table.source.load or "full",
            "sample_size": table.source.sample_size,
        }
        for table in loaded_plan.tables
        if table.mode == "reuse" and table.source
    }
    if bindings:
        for name_, reference in load_for_plan(
            ConnectionRegistry(), bindings, seed=loaded_plan.meta.seed
        ).items():
            reference_sets.add(name_, reference.keys, reference.attributes)
            console.print(f"[dim]loaded {reference.size:,} keys for {name_}[/dim]")

    from sdg.engine.execute import NeverGenerated

    from sdg.engine.execute import refuse_never_generated

    try:
        refuse_never_generated(loaded_plan)
        result = dry_run(loaded_plan, rows=rows, reference_sets=reference_sets)
    except NeverGenerated as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(format_preview(result.frames, rows=rows))
    console.print(
        "[dim]counts: "
        + ", ".join(f"{k.split('.')[-1]}={v:,}" for k, v in result.counts().items())
        + "[/dim]"
    )


def _load_plan_and_reference_sets(seed_override: str | None = None):
    """The plan, plus the keys of every table it reuses."""
    from sdg.engine.execute import ReferenceSets
    from sdg.plan.model import Plan as PlanModel
    from sdg.plan.refsets import load_for_plan

    loaded = read_work_file(PlanModel.load, work_dir() / "plan.json", "plan")
    if seed_override:
        loaded.meta.seed = seed_override

    sets = ReferenceSets()
    sizes: dict[str, int] = {}
    bindings = {
        table.name: {
            "connection": table.source.connection,
            "table": table.source.table,
            "schema": table.source.schema_name,
            "key_column": table.source.key_columns[0],
            "filter": table.source.filter,
            "load": table.source.load or "full",
            "sample_size": table.source.sample_size,
        }
        for table in loaded.tables
        if table.mode == "reuse" and table.source
    }
    if bindings:
        for name, reference in load_for_plan(
            ConnectionRegistry(), bindings, seed=loaded.meta.seed
        ).items():
            sets.add(name, reference.keys, reference.attributes)
            sizes[name] = reference.size
    return loaded, sets, sizes


@app.command()
def generate(
    seed: str = typer.Option(None, help="override the plan's seed"),
    target: str = typer.Option("target", help="connection to write to"),
    csv: Path = typer.Option(None, "--csv", help="also write a CSV file per table"),
    dry: bool = typer.Option(False, "--dry", help="generate but do not write to the database"),
    disable_triggers: bool = typer.Option(
        False, "--disable-triggers", help="turn triggers off around the load"
    ),
) -> None:
    """Generate the planned rows and write them to the target."""
    from sdg.config import DATABASE_GENERATION_OFF, database_generation
    from sdg.engine.execute import NeverGenerated, refuse_never_generated, run_plan
    from sdg.plan.validator import blocking
    from sdg.writers.csvfile import write_run

    refuse_if_work_belongs_elsewhere()
    # Off for this installation: CSV files can still be made; nothing is
    # written to a database.
    if not database_generation() and not dry:
        if not csv:
            console.print(f"[red]{DATABASE_GENERATION_OFF}[/red]")
            console.print("Give --csv to write the rows to files instead.")
            raise typer.Exit(code=1)
        dry = True

    loaded, sets, sizes = _load_plan_and_reference_sets(seed)

    stoppers = blocking(loaded.warnings)
    if stoppers:
        console.print("[red]The plan has blocking warnings; nothing was generated.[/red]")
        for warning in stoppers:
            console.print(f"  [red]{warning.code}[/red]: {warning.message}")
        raise typer.Exit(code=1)

    for name, size in sizes.items():
        console.print(f"[dim]loaded {size:,} keys for {name}[/dim]")

    if dry:
        console.print(f"generating with seed [cyan]{loaded.meta.seed}[/cyan] ...")
        try:
            refuse_never_generated(loaded)
            result = run_plan(loaded, reference_sets=sets)
        except NeverGenerated as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"generated {sum(result.counts().values()):,} rows in {result.seconds:.1f}s"
        )
        if csv:
            written = write_run(result, loaded, csv)
            console.print(f"wrote {len(written)} CSV files to {csv}")
        console.print(
            "[yellow]--dry was given, so nothing was written to the database.[/yellow]"
            if database_generation() else
            "[yellow]Generating into a database is turned off for this installation, "
            "so only the files were written.[/yellow]")
        return

    # Into a database the way the screens load: key starts read from the
    # target, the manifest merged so cleanup finds every run, and the load
    # kept as a generation request for audit.
    from sdg.api.app import _record_load
    from sdg.api.service import Service, ServiceError

    if target != "target":
        raise typer.BadParameter(
            "Loads go to the target connection; choose it in .env or on the screens.")
    loader = Service(work_dir=work_dir())

    def written(planned, ran, result, report, references, partial):
        if csv:
            files = write_run(result, ran, csv)
            console.print(f"wrote {len(files)} CSV files to {csv}")
        return _record_load(planned, ran, result, report, references, partial,
                            loaded_by=loader)

    console.print(f"generating with seed [cyan]{seed or loaded.meta.seed}[/cyan] ...")
    try:
        outcome = loader.generate(written, seed=seed, disable_triggers=disable_triggers)
    except (ServiceError, RuntimeError, PermissionError, NeverGenerated) as exc:
        # Refusals said as sentences: a protected database (PermissionError),
        # generation turned off or key starts that could not be read
        # (RuntimeError), a table the pack never generates, and the service's own.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Written to {loader.registry.database_for('target')}")
    table.add_column("table", overflow="fold")
    table.add_column("rows", justify="right")
    table.add_column("seconds", justify="right")
    for record in outcome["tables"]:
        table.add_row(record["table"], f"{record['rows_written']:,}",
                      f"{record['seconds']:.2f}")
    console.print(table)
    console.print(f"total {outcome['rowsWritten']:,} rows in {outcome['seconds']:.1f}s")
    for moved in outcome.get("keysAdvanced") or []:
        console.print(f"[dim]{moved['table']}.{moved['column']} starts at "
                      f"{moved['started']}, past the rows already there[/dim]")
    request = outcome.get("request") or {}
    if request.get("request"):
        console.print(f"kept as generation request {request['request']}")
    console.print(f"wrote {loader.path('manifest')}")


@app.command(name="validate")
def validate_command(
    target: str = typer.Option("target", help="connection to read back from"),
) -> None:
    """Check the generated data against every rule the plan claimed."""
    from sdg.discovery.model import PhysicalModel
    from sdg.plan.model import Plan as PlanModel
    from sdg.writers.manifest import save_manifest
    from sdg.writers.validate import validate_run

    manifest_path = work_dir() / "run-manifest.json"
    manifest = read_run_manifest(manifest_path, "Run `sdg generate` first.")

    loaded = read_work_file(PlanModel.load, work_dir() / "plan.json", "plan")
    model = read_work_file(PhysicalModel.load, work_dir() / "physical-model.json", "discover")
    _plan, sets, _sizes = _load_plan_and_reference_sets()

    report = validate_run(
        ConnectionRegistry(), loaded, model, manifest,
        connection=target, reference_sets=sets.keys,
    )

    table = Table(title="Post-run validation")
    table.add_column("check", overflow="fold")
    table.add_column("kind")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for check in report.checks:
        table.add_row(
            check.name, check.kind,
            "[green]pass[/green]" if check.passed else "[red]FAIL[/red]",
            check.detail,
        )
    console.print(table)

    summary = report.summary()
    console.print(
        f"\n[bold]{summary['checks'] - summary['failed']} of {summary['checks']} checks "
        f"passed[/bold]"
    )
    manifest["validation"] = {
        **summary,
        "checks": [c.as_record() for c in report.checks],
    }
    save_manifest(manifest, manifest_path)

    if not report.passed:
        for check in report.failures():
            console.print(f"  [red]{check.name}[/red]: {check.detail}")
        raise typer.Exit(code=1)
    console.print("[green]Validation passed.[/green]")


@app.command()
def cleanup(
    target: str = typer.Option("target", help="connection to delete from"),
    yes: bool = typer.Option(False, "--yes", help="do not ask before deleting"),
) -> None:
    """Delete exactly the rows the last run wrote."""
    from sdg.writers.sqlserver import (
        WrongDatabase,
        check_cleanup_target,
        cleanup as run_cleanup,
        manifest_key_columns,
        trigger_side_effects,
    )

    refuse_if_work_belongs_elsewhere()
    manifest_path = work_dir() / "run-manifest.json"
    manifest = read_run_manifest(manifest_path, "There is no run to clean up.")

    planned = [
        (t.get("table", "?"), t.get("rows_written"))
        for t in manifest["tables"]
        if manifest_key_columns(t)
    ]
    registry = ConnectionRegistry()
    try:
        database = check_cleanup_target(registry, manifest, target)
    except WrongDatabase as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    server = registry.settings_for(target).endpoint
    plan_record = manifest.get("plan")
    seed = plan_record.get("seed") if isinstance(plan_record, dict) else None
    console.print(f"This will delete rows written by seed {seed or '(not recorded)'} "
                  f"from {database} on {server}:")
    for name, rows in planned:
        count = f"up to {rows:,} rows" if isinstance(rows, int) else "rows written by the run"
        console.print(f"  {name}: {count}")

    fired = trigger_side_effects(manifest)
    if fired:
        console.print(
            "\n[yellow]These triggers were live during the load:[/yellow] "
            + ", ".join(fired)
        )
        console.print(
            "[yellow]Whatever they wrote into other tables stays. Those rows were "
            "written by the database, not by this run, so cleanup does not remove "
            "them.[/yellow]"
        )

    if not yes:
        confirmed = typer.confirm(f"Delete these rows from {database}?")
        if not confirmed:
            console.print("Nothing was deleted.")
            raise typer.Exit(code=0)

    deleted = run_cleanup(registry, manifest, connection=target)
    for name, count in deleted.items():
        console.print(f"  deleted {count:,} rows from {name}")
    console.print(f"[green]removed {sum(deleted.values()):,} rows[/green]")

    # replace() rather than rename(): on Windows a rename onto an existing file
    # fails, and a second cleanup should not error after it has already deleted.
    archived = manifest_path.with_suffix(".cleaned.json")
    manifest_path.replace(archived)
    console.print(f"manifest moved to {archived}")


@app.command()
def demo(
    small: bool = typer.Option(False, "--small", help="use the 1 percent fixture volumes"),
    subscribers: int = typer.Option(None, help="root row count; defaults to the scale"),
    seed: str = typer.Option("demo-seed-001", help="named seed for the run"),
    keep: bool = typer.Option(False, "--keep", help="leave the generated rows in place"),
    catalog: Path = typer.Option(None, "--catalog", help="catalog YAML to use"),
) -> None:
    """Run connect through validate in one go and print the success measures."""
    import time

    from sdg.catalog.loader import load_catalog
    from sdg.discovery.model import PhysicalModel
    from sdg.discovery.run import discover as run_discovery
    from sdg.engine.dryrun import dry_run, format_preview
    from sdg.mapping.scorer import map_columns
    from sdg.plan.build import (
        DEFAULT_TABLE_MODES, BuildInputs, build_plan, default_reference_sets,
    )
    from sdg.plan.combinations import discover as discover_combinations
    from sdg.plan.detect import detect_sum_expressions
    from sdg.plan.hierarchy import describe
    from sdg.plan.proposer import propose
    from sdg.plan.validator import blocking
    from sdg.profiling.run import profile as run_profile
    from sdg.review.page import build_context, write_page
    from sdg.review.queue import build_queue, decisions_template, load_decisions, save_decisions
    from sdg.writers.manifest import build_manifest, save_manifest
    from sdg.writers.sqlserver import PartialWrite, cleanup as run_cleanup, write
    from sdg.writers.validate import validate_run

    refuse_if_work_belongs_elsewhere()
    # A run not yet cleaned up has its record in run-manifest.json, the only
    # thing that can find its rows. The demo removes its own rows from a
    # record of its own; kept rows would need that file, so --keep waits.
    run_record = work_dir() / "run-manifest.json"
    if keep and run_record.exists():
        console.print(f"[red]{run_record} records rows from an earlier run that have not "
                      "been removed. Run `sdg cleanup` first, or run the demo without "
                      "--keep. Nothing was generated.[/red]")
        raise typer.Exit(code=1)

    started = time.perf_counter()
    registry = ConnectionRegistry()
    steps: list[tuple[str, float]] = []

    def step(label: str, mark: float) -> float:
        now = time.perf_counter()
        steps.append((label, now - mark))
        console.print(f"[dim]{label} in {now - mark:.1f}s[/dim]")
        return now

    root_count = subscribers if subscribers is not None else (200 if small else 5_000)

    console.rule("[bold]connect")
    for name in ("source", "ref"):
        result = registry.check(name)
        state = "[green]ok[/green]" if result["ok"] else f"[red]{result.get('error')}[/red]"
        console.print(f"  {name} -> {result.get('db', '?')} {state}")
        if not result["ok"]:
            raise typer.Exit(code=1)
    mark = step("connected", started)

    console.rule("[bold]discover")
    discovery = run_discovery(registry, "source")
    discovery.model.save(work_dir() / "physical-model.json")
    summary = discovery.summary()
    console.print(
        f"  {summary['tables']} tables, {summary['columns']} columns, "
        f"{summary['declared_foreign_keys']} declared and "
        f"{summary['inferred_foreign_keys']} inferred foreign keys"
    )
    for key in discovery.inferred:
        console.print(
            f"  [yellow]inferred[/yellow] {key.child_table}.{key.child_column} -> "
            f"{key.parent_table}.{key.parent_column} [dim]({key.coverage:.1%})[/dim]"
        )
    mark = step("discovered", mark)

    console.rule("[bold]profile")
    report = run_profile(registry, discovery.model)
    report.save(work_dir() / "profile.json")
    console.print(
        f"  {len(report.columns)} columns measured, "
        f"{sum(1 for c in report.columns if c.fitted_format)} formats fitted, "
        f"{len(report.dependencies)} dependencies, {len(report.orderings)} date orderings, "
        f"{len(report.ratios)} ratios"
    )
    mark = step("profiled", mark)

    console.rule("[bold]map")
    loaded_catalog = load_catalog(catalog)
    mappings = map_columns(discovery.model, loaded_catalog, report)
    mappings.save(work_dir() / "mappings.json")
    stats = mappings.statistics()
    console.print(
        f"  [green]{stats['auto_map_rate']:.0%}[/green] of {stats['mappable_columns']} "
        f"mappable columns auto-mapped at high confidence"
    )
    mark = step("mapped", mark)

    console.rule("[bold]review")
    sums = detect_sum_expressions(registry, discovery.model)
    proposals = propose(discovery.model, report, sums, mappings, loaded_catalog)
    queue = build_queue(discovery.model, mappings, proposals)
    decisions_path = work_dir() / "review-decisions.json"
    if not decisions_path.exists():
        save_decisions(decisions_template(queue), decisions_path)
    page = write_page(
        build_context(
            discovery.model, report, mappings, proposals, queue, loaded_catalog,
            decisions_path=str(decisions_path),
        ),
        work_dir() / "review.html",
    )
    high = sum(1 for p in proposals if p.confidence == "high")
    console.print(f"  {high} of {len(proposals)} rules proposed at high confidence")
    console.print(f"  review queue: [cyan]{len(queue)}[/cyan] columns")
    for found in sums:
        console.print(f"  [cyan]expression[/cyan] {found.parent_table}.{found.parent_column} = {found.expression()}")
    console.print(f"  page: {page}")
    mark = step("reviewed", mark)

    console.rule("[bold]plan")
    inputs = BuildInputs(
        model=discovery.model, catalog=loaded_catalog, mappings=mappings,
        proposals=proposals, report=report,
        decisions=load_decisions(decisions_path),
        modes=dict(DEFAULT_TABLE_MODES),
        root_counts={"dbo.CMC_SBSB_SUBSC": root_count},
        seed=seed, name="facets-slice-demo",
        catalog_path=str(catalog) if catalog else None,
    )
    inputs.reference_sets = default_reference_sets(registry, inputs)
    inputs.combinations = discover_combinations(registry)
    built, graph = build_plan(inputs)
    built.save(work_dir() / "plan.json")
    for line in describe(graph):
        console.print(f"  {line}")
    for reference in inputs.reference_sets.values():
        console.print(f"  [dim]{reference.table}: {reference.describe()}[/dim]")
    stoppers = blocking(built.warnings)
    if stoppers:
        for warning in stoppers:
            console.print(f"  [red]{warning.code}[/red]: {warning.message}")
        raise typer.Exit(code=1)
    mark = step("planned", mark)

    console.rule("[bold]dry run")
    preview = dry_run(built, rows=3, reference_sets=_sets_from(inputs))
    console.print(format_preview(preview.frames, rows=3))
    mark = step("dry run", mark)

    console.rule("[bold]generate")
    from sdg.engine.execute import run_plan

    result = run_plan(built, reference_sets=_sets_from(inputs))
    model = PhysicalModel.load(work_dir() / "physical-model.json")
    # A load that fails partway has committed the tables before it; its record
    # is kept and those rows removed below, like a finished load's.
    failed: PartialWrite | None = None
    try:
        write_report = write(registry, built, result, model, connection="target")
    except PartialWrite as exc:
        failed, write_report = exc, exc.report
    manifest = build_manifest(
        built, write_report,
        connection="target", database=registry.database_for("target"),
        server=registry.settings_for("target").endpoint,
        reference_sizes={t: r.size for t, r in inputs.reference_sets.items()},
    )
    manifest_path = save_manifest(
        manifest, run_record if keep else work_dir() / "demo-run-manifest.json")

    # The demo's record is its own file, which `sdg cleanup` does not read: its
    # rows are removed however the steps after the load end, a failed load or
    # Ctrl-C included. With --keep the record is run-manifest.json instead.
    try:
        for record in write_report.tables:
            console.print(f"  {record.table}: {record.rows_written:,} rows")
        console.print(f"  total [green]{write_report.total_rows():,}[/green] rows")
        if failed is not None:
            console.print(f"[red]The load stopped partway: {failed}[/red]")
            if keep:
                console.print(f"The rows above are recorded in {manifest_path}; "
                              "run `sdg cleanup` to remove them.")
            raise typer.Exit(code=1)
        mark = step("generated", mark)

        console.rule("[bold]validate")
        validation = validate_run(
            registry, built, model, manifest,
            connection="target", reference_sets=_sets_from(inputs).keys,
        )
        validation_summary = validation.summary()
        for check in validation.failures():
            console.print(f"  [red]{check.name}[/red]: {check.detail}")
        console.print(
            f"  [green]{validation_summary['checks'] - validation_summary['failed']}"
            f"[/green] of {validation_summary['checks']} checks passed"
        )
        manifest["validation"] = validation_summary
        save_manifest(manifest, manifest_path)
        mark = step("validated", mark)
    finally:
        if not keep:
            console.rule("[bold]cleanup")
            deleted = run_cleanup(registry, manifest, connection="target")
            console.print(f"  removed {sum(deleted.values()):,} rows; the fixture is as it was")
            manifest_path.replace(manifest_path.with_suffix(".cleaned.json"))
            mark = step("cleaned up", mark)

    elapsed = time.perf_counter() - started
    console.rule("[bold]summary")
    table = Table(title="Proof-of-concept success measures")
    table.add_column("measure", overflow="fold")
    table.add_column("target")
    table.add_column("result")
    table.add_row(
        "auto-map rate at high confidence", "60% or more",
        f"[green]{stats['auto_map_rate']:.0%}[/green]",
    )
    table.add_row(
        "review queue size", "under 20 columns",
        f"[green]{len(queue)}[/green]",
    )
    claims_ratio = report.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER")
    table.add_row(
        "claims per member, share with none", "within 2 points of 30%",
        f"[green]{claims_ratio.zero_share:.0%}[/green]" if claims_ratio else "-",
    )
    table.add_row(
        "claims per member, median among those with claims", "5",
        f"[green]{claims_ratio.median_nonzero:g}[/green]" if claims_ratio else "-",
    )
    table.add_row(
        "post-run validation", "passes",
        "[green]passed[/green]" if validation.passed
        else f"[red]{validation_summary['failed']} failed[/red]",
    )
    table.add_row("rows generated", "-", f"{write_report.total_rows():,}")
    table.add_row(
        "end to end", "under 10 minutes",
        f"[green]{elapsed:.0f}s[/green]" if elapsed < 600 else f"[red]{elapsed:.0f}s[/red]",
    )
    console.print(table)
    console.print("\n[bold]Timings[/bold]  " + "  ".join(f"{n} {s:.1f}s" for n, s in steps))
    console.print(f"\nReview page: {page}")

    if not validation.passed:
        raise typer.Exit(code=1)


@app.command()
def ui(
    port: int = typer.Option(8777, help="port to serve on"),
    host: str = typer.Option("127.0.0.1", help="address to bind"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="open a browser"),
    reload: bool = typer.Option(False, "--reload", help="restart when the code changes"),
) -> None:
    """Serve the web interface."""
    import uvicorn

    from sdg.api.app import UI_DIST

    if not UI_DIST.exists():
        console.print(
            f"[red]The interface has not been built.[/red]\n"
            f"Run [bold]npm install && npm run build[/bold] in {UI_DIST.parent}, "
            f"then try again."
        )
        raise typer.Exit(code=1)

    # Each session starts from fresh working files. The query and clustering
    # results held under sdg/analyzer/cache are rebuilt on first use and kept
    # for the rest of the session; carried across sessions they would describe
    # data that may have been reseeded or generated into since.
    from sdg.analyzer.services import disk_cache

    swept = disk_cache.clear()
    if swept.get("removed_files"):
        console.print(
            f"[dim]cleared {swept['removed_files']} working files "
            f"({swept['freed_bytes'] / 2**20:.0f} MB) from the last session[/dim]"
        )

    address = f"http://{host}:{port}"
    console.print(f"[bold]SDM[/bold] on [cyan]{address}[/cyan]")
    console.print(f"[dim]reading {get_settings().source_db}; the API is at {address}/api/docs[/dim]")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(address)).start()

    uvicorn.run(
        "sdg.api.app:app" if reload else _import_app(),
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@app.command()
def patterns(
    file: Path = typer.Argument(None, help="analyzer output, as JSON"),
    catalog: Path = typer.Option(None, "--catalog", help="catalog YAML to match labels against"),
    as_of: str = typer.Option(
        None, "--as-of", help="reference date for ages, as YYYY-MM-DD"
    ),
    only: str = typer.Option(None, "--id", help="resolve just this pattern from the file"),
    apply_: bool = typer.Option(
        False, "--apply", help="write the resolved targets into review-decisions.json"
    ),
    clear: bool = typer.Option(
        False, "--clear", help="remove entries a pattern wrote, leaving hand-made ones"
    ),
) -> None:
    """Map an analyzer pattern onto the columns it governs.

    Where a pattern parameter maps to a column, the pattern supplies the value.
    Every other column keeps the rule the tool worked out. Nothing is written
    until `--apply`, and `sdg plan` is what picks the result up.
    """
    from sdg.catalog.loader import load_catalog
    from sdg.discovery.model import PhysicalModel
    from sdg.mapping.scorer import MappingStore
    from sdg.plan.model import Plan
    from sdg.plan.patterns import (
        PatternError,
        apply_to_decisions,
        clear_from_decisions,
        load_document,
        resolve_document,
    )
    from sdg.review.queue import load_decisions, save_decisions

    if apply_ or clear:
        refuse_if_work_belongs_elsewhere()
    directory = work_dir()
    decisions_path = directory / "review-decisions.json"

    if clear:
        document, removed = clear_from_decisions(load_decisions(decisions_path))
        save_decisions(document, decisions_path)
        console.print(f"removed {removed} pattern entries from {decisions_path}")
        if not file:
            return

    if not file:
        raise typer.BadParameter("name the analyzer JSON to resolve, or pass --clear")

    for name, step in (("physical-model.json", "discover"), ("mappings.json", "map"),
                       ("plan.json", "plan")):
        if not (directory / name).exists():
            raise typer.BadParameter(f"missing {directory / name}. Run `sdg {step}` first.")

    model = read_work_file(PhysicalModel.load, directory / "physical-model.json", "discover")
    mappings = read_work_file(MappingStore.load, directory / "mappings.json", "map")
    plan_ = read_work_file(Plan.load, directory / "plan.json", "plan")
    loaded = load_catalog(catalog) if catalog else load_catalog()

    try:
        found = load_document(file)
        if only:
            found.patterns = [p for p in found.patterns if p.id == only]
            if not found.patterns:
                raise PatternError(f"{file} holds no pattern called {only}")
        resolutions = resolve_document(
            found, catalog=loaded, mappings=mappings, plan=plan_, model=model, as_of=as_of
        )
    except PatternError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    loose = 0
    for resolution in resolutions:
        heading = f"{resolution.pattern} - {resolution.label or resolution.kind}"
        support = (
            f"  [dim]support {resolution.support:.1%}[/dim]"
            if resolution.support is not None else ""
        )
        console.print(f"\n[bold]{heading}[/bold]{support}")
        console.print(
            f"[dim]subject {resolution.subject}; "
            f"{resolution.bound()} of "
            f"{resolution.bound() + len(resolution.rejections)} parameters bound[/dim]"
        )

        if resolution.targets:
            table = Table(title="Where the pattern supplies the value")
            table.add_column("column", overflow="fold")
            table.add_column("from")
            table.add_column("rule", overflow="fold")
            table.add_column("replaces")
            table.add_column("where")
            for target in resolution.targets:
                where = (
                    "the subject" if target.relation == "subject"
                    else f"{target.depth} above" if target.relation == "ancestor"
                    else f"{target.depth} below"
                )
                if target.apply == "at_least_one":
                    loose += 1
                    where += ", one row"
                table.add_row(
                    target.qualified,
                    target.parameter,
                    f"{target.generator} {json.dumps(target.params, default=str)[:60]}",
                    target.replaces.get("generator", "-"),
                    where,
                )
            console.print(table)

        for rejection in resolution.rejections:
            console.print(f"  [yellow]not bound[/yellow] {rejection.parameter}")
            console.print(f"    [dim]{rejection.reason}[/dim]")
            if rejection.candidates:
                console.print(f"    [dim]nearest: {', '.join(rejection.candidates)}[/dim]")

    path = write_artifact(
        "pattern-targets.json",
        [r.model_dump(mode="json") for r in resolutions],
    )
    console.print(f"\nwrote {path}")

    if loose:
        console.print(
            f"[yellow]{loose} target(s) the analyzer meant for one row only will be "
            f"applied to every row[/yellow] [dim]- a share of the population is not "
            f"something this mode can express yet[/dim]"
        )

    if not apply_:
        console.print("[dim]nothing written to the plan; pass --apply to do that[/dim]")
        return

    usable = [r for r in resolutions if r.is_usable()]
    if not usable:
        console.print("[red]nothing to apply: no parameter bound to a column[/red]")
        raise typer.Exit(code=1)

    document, written = apply_to_decisions(usable, load_decisions(decisions_path))
    save_decisions(document, decisions_path)
    console.print(f"wrote {len(written)} column overrides into {decisions_path}")
    console.print("[dim]run `sdg plan` to build with them, then `sdg generate`[/dim]")


# ---------------------------------------------------------------------------
# Files for a pattern, as Generation writes them
# ---------------------------------------------------------------------------

def _files_dir(out: Path | None) -> Path:
    directory = out or (work_dir() / "files")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _named_for(filename: str, request_id: int | None) -> str:
    """As the screens name a download: 834-request-12.txt."""
    if request_id is None or f"request-{request_id}" in filename:
        return filename
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}-request-{request_id}.{ext}" if dot and stem else f"{filename}-request-{request_id}"


def _save(parts, path: Path) -> tuple[int, str]:
    """Write the file's parts to `path`; its size and fingerprint. A file that
    fails part way is removed, never left looking whole."""
    import hashlib

    fingerprint, size = hashlib.sha256(), 0
    try:
        with path.open("wb") as handle:
            for part in parts:
                data = part.encode("utf-8") if isinstance(part, str) else part
                handle.write(data)
                fingerprint.update(data)
                size += len(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return size, fingerprint.hexdigest()


def _refused(exc: Exception) -> typer.Exit:
    from fastapi import HTTPException

    from sdg.discovery.errors import plain

    said = exc.detail if isinstance(exc, HTTPException) else plain(exc)
    console.print(f"[red]{said}[/red]")
    return typer.Exit(code=1)


@app.command("write-file")
def write_file_command(
    pattern: int = typer.Option(..., "--pattern", help="the saved pattern's id"),
    format_: str = typer.Option(
        "csv", "--format",
        help="csv, fixed (with --layout), edi:834, edi:837P, edi:835, or sample:<config id>"),
    members: int = typer.Option(100, "--members", help="new members, for a member file"),
    claims: bool = typer.Option(False, "--claims", help="claims for the pattern's members"),
    rows: int = typer.Option(None, "--rows", help="at most this many records"),
    layout: int = typer.Option(None, "--layout", help="the layout, for a fixed or delimited file"),
    per_transaction: int = typer.Option(None, "--per-transaction",
                                        help="EDI: members or claims in one ST/SE set"),
    as_of: str = typer.Option(None, "--as-of", help="the date ages are as of, YYYY-MM-DD"),
    request: int = typer.Option(None, "--request",
                                help="add the file to this request: the same people"),
    out: Path = typer.Option(None, "--out", help="the folder to write into (work/files)"),
) -> None:
    """Write a file for a saved pattern, as Generation does: kept under a
    request, so `sdg write-again` makes the same file from the record alone."""
    from datetime import datetime, timezone

    from sdg.api.app import ExportRequest, write_file

    asked: dict = {"format": format_, "pattern_id": pattern, "members": members,
                   "subject": "claims" if claims else "members", "rows": rows,
                   "layout_id": layout, "per_transaction": per_transaction, "record": True}
    if as_of:
        asked["as_of"] = as_of
    if request is not None:
        asked["request_id"] = request
    else:
        asked["request_started"] = (datetime.now(timezone.utc)
                                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    try:
        parts, _media, filename, request_id = write_file(ExportRequest(**asked))
        path = _files_dir(out) / _named_for(filename, request_id)
        size, _ = _save(parts, path)
    except Exception as exc:
        raise _refused(exc) from exc
    console.print(f"wrote {path} ({size:,} bytes)"
                  + (f", kept under request {request_id}" if request_id is not None else ""))


@app.command("write-again")
def write_again_command(
    file_id: int = typer.Argument(..., help="the kept file's id (sdg requests lists them)"),
    out: Path = typer.Option(None, "--out", help="the folder to write into (work/files)"),
) -> None:
    """Write a kept file again from its request's record alone, and say whether
    it came out the same."""
    from sdg.api.app import _regenerated_load, original_file, write_again

    try:
        original = original_file(file_id)
        directory = _files_dir(out)
        if original["format"] == "database":
            response = _regenerated_load(original)
            named = response.headers.get("content-disposition", "").split('filename="')[-1]
            path = directory / (named.rstrip('"') or f"load-{file_id}.zip")
            path.write_bytes(response.body)
            console.print(f"regenerated the load's rows as {path}; no database was written")
            return
        parts, _media = write_again(original)
        # The stored name came from a browser: its own name only, never a path.
        name = Path(str(original["file_name"]).replace("\\", "/")).name or "sdm-file"
        stem, dot, ext = name.rpartition(".")
        path = directory / (f"{stem}-again.{ext}" if dot and stem else f"{name}-again")
        size, fingerprint = _save(parts, path)
    except Exception as exc:
        raise _refused(exc) from exc
    same = fingerprint == original["fingerprint"]
    console.print(f"wrote {path} ({size:,} bytes): "
                  + ("[green]the same file[/green]" if same else "[red]it differs[/red]"))
    if not same:
        raise typer.Exit(code=1)


@app.command("requests")
def requests_command(
    day: str = typer.Option(None, "--day", help="written on this day, YYYY-MM-DD"),
    pattern: int = typer.Option(None, "--pattern", help="for this saved pattern"),
    user: str = typer.Option(None, "--user", help="started by this person"),
    purge_unsaved_samples: bool = typer.Option(
        False, "--purge-unsaved-samples",
        help="list the runs of unsaved configurations that still keep their sample's "
             "text (kept before 2026-11-06); with --apply, keep only its digest and facts"),
    apply: bool = typer.Option(False, "--apply",
                               help="with --purge-unsaved-samples: make the change"),
) -> None:
    """Generation requests and the files kept under them, newest first."""
    from sdg.analyzer.services import generation_requests as gr

    if apply and not purge_unsaved_samples:
        raise typer.BadParameter("--apply goes with --purge-unsaved-samples")
    if purge_unsaved_samples:
        _purge_unsaved_samples(apply)
        return
    try:
        found = gr.list_requests(day=day, pattern_id=pattern, user=user)
    except Exception as exc:
        raise _refused(exc) from exc
    if not found:
        console.print("no requests match")
        return
    table = Table(title="Generation requests")
    for column in ("request", "pattern", "file id", "file", "written", "again"):
        table.add_column(column)
    for item in found:
        for file in item["files"] or [{}]:
            again = ("" if file.get("replay_of") is None
                     else "same" if file.get("matched") else "differs")
            table.add_row(str(item["id"]), str(item.get("pattern_name") or item["pattern_key"]),
                          str(file.get("id", "")), str(file.get("file_name", "")),
                          str(file.get("written_at", ""))[:19], again)
    console.print(table)


def _purge_unsaved_samples(apply: bool) -> None:
    """Rewrite (with --apply) or list (dry run) the unsaved-configuration
    snapshots that still hold a sample's text; every change is logged in
    work/unsaved-sample-purge.jsonl before it is made."""
    from sdg.api.app import UNSAVED_PURGE_LOG, purge_unsaved_samples, service

    try:
        done = purge_unsaved_samples(apply=apply)
    except Exception as exc:
        raise _refused(exc) from exc
    if not done:
        console.print("no run of an unsaved configuration keeps its sample: nothing to do")
        return
    table = Table(title=("Rewritten" if apply else "Would be rewritten (dry run)")
                  + ": runs of unsaved configurations that keep their sample")
    for column in ("snapshot", "sample", "source", "bytes before", "bytes after"):
        table.add_column(column)
    for entry in done:
        source = entry["source"]
        said = (f"standard {source['standard']}" if source and "standard" in source
                else f"configuration {source['config']}" if source else "uploaded (none)")
        table.add_row(entry["digest"], str(entry.get("name") or ""), said,
                      f"{entry['size_before']:,}", f"{entry['size_after']:,}")
    console.print(table)
    if apply:
        console.print(f"{len(done)} snapshot(s) rewritten; logged in "
                      f"{service.work / UNSAVED_PURGE_LOG}")
    else:
        console.print(f"{len(done)} snapshot(s) would be rewritten. Nothing was changed: "
                      "run again with --apply to make the change.")


def _import_app():
    from sdg.api.app import app as api_app

    return api_app


def _sets_from(inputs):
    """Reference sets in the shape the engine wants."""
    from sdg.engine.execute import ReferenceSets

    sets = ReferenceSets()
    for name, reference in inputs.reference_sets.items():
        if not name.startswith("element:"):
            sets.add(name, reference.keys, reference.attributes)
    return sets


if __name__ == "__main__":
    app()
