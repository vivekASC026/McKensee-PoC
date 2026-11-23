"""Assemble a generation plan from everything the earlier steps produced.

This is where the contract gets written: the physical model, the profile, the
mappings, the proposals, and the user's decisions all resolve into one document
that says, for every column, exactly what will be generated and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sdg.catalog.model import Catalog
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import PhysicalModel
from sdg.mapping.scorer import MappingStore
from sdg.plan import hierarchy as hierarchy_module
from sdg.plan.hierarchy import Hierarchy
from sdg.plan.model import (
    Cohort,
    Evidence,
    FieldOverride,
    HistogramBin,
    Plan,
    PlanTable,
    Ratio,
    ReferenceSetRecord,
    Relationship,
    Source,
    Warning_,
    new_meta,
)
from sdg.plan.proposer import Proposal
from sdg.plan.refsets import ReferenceSet, load_for_plan
from sdg.plan.resolver import decisions_index, element_index, resolve_column
from sdg.plan.validator import validate
from sdg.profiling.model import ProfileReport
from sdg.packs import held

DEFAULT_MODE = "generate"

# Providers are real: a run points claims at the providers that exist and
# never invents one, whatever a setting or a flag asks for.
NEVER_GENERATED = frozenset(held("pack:tables.never_generated"))

# What a run does with each table unless somebody chose otherwise. The
# screens and the command line both start from this.
DEFAULT_TABLE_MODES: dict[str, str] = held("pack:tables.modes")


@dataclass
class BuildInputs:
    model: PhysicalModel
    catalog: Catalog
    mappings: MappingStore
    proposals: list[Proposal]
    report: ProfileReport | None = None
    decisions: dict[str, Any] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    root_counts: dict[str, int] = field(default_factory=dict)
    driving_parents: dict[str, str] = field(default_factory=dict)
    ratio_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference_sets: dict[str, ReferenceSet] = field(default_factory=dict)
    # Features that only occur in certain combinations, and those
    # combinations, from the pattern store. Empty where there is none.
    combinations: list[Any] = field(default_factory=list)
    seed: str = "demo-seed-001"
    name: str = "facets-slice"
    catalog_path: str | None = None


def table_modes(inputs: BuildInputs) -> dict[str, str]:
    """Mode per table: what the user asked for, otherwise generate.

    A decision made earlier is the starting point, and what this invocation
    asked for wins over it. That way a mode set in the browser is still in
    force at a prompt, and a flag typed at that prompt still overrides it.
    """
    declared: dict[str, str] = {}
    for entry, setting in (inputs.decisions.get("tables") or {}).items():
        if isinstance(setting, dict) and setting.get("mode"):
            declared[entry] = setting["mode"]
    declared.update(inputs.modes)
    modes = {t.name: declared.get(t.name, DEFAULT_MODE) for t in inputs.model.tables}
    # A never-generated table defaults to reuse, but Generate asked for it
    # stays in the plan: its shape can be modeled and a driving parent chosen.
    # Every run that makes rows refuses it (engine.execute.refuse_never_generated).
    for name in NEVER_GENERATED & modes.keys():
        if name not in declared and modes[name] == "generate":
            modes[name] = "reuse"
    return modes


def table_ratios(inputs: BuildInputs) -> dict[str, dict[str, Any]]:
    """Stated children-per-parent shapes, per table.

    These live beside the table's mode in the decisions file rather than in the
    interface's settings, because a run started in a browser has to be
    finishable at a prompt. The settings map is still honored so the interface
    can pass one in directly.
    """
    stated: dict[str, dict[str, Any]] = {}
    for entry, setting in (inputs.decisions.get("tables") or {}).items():
        if isinstance(setting, dict) and setting.get("ratio"):
            stated[entry] = setting["ratio"]
    stated.update(inputs.ratio_overrides)
    return stated


def ratio_for(
    report: ProfileReport | None,
    child: str,
    parent: str,
    override: dict[str, Any] | None = None,
) -> tuple[Ratio | None, float | None]:
    """Turn a profiled ratio into the plan's ratio, and its mean.

    An override replaces the measured shape outright rather than adjusting it,
    because the two are not comparable: a histogram is what the source does,
    and a fixed or range ratio is what someone decided instead.
    """
    if override and override.get("kind") in ("fixed", "range"):
        ratio = _stated_ratio(parent, override)
        return ratio, ratio.mean_children()

    if report is None:
        return None, None
    profiled = report.ratio_for(child, parent)
    if profiled is None:
        return None, None

    evidence = [Evidence(kind="ratio_histogram", detail=profiled.evidence, coverage=1.0)]
    if profiled.proposal.get("kind") == "fixed":
        ratio = Ratio(
            parent=parent,
            kind="fixed",
            value=round(profiled.mean, 4),
            zero_share=profiled.zero_share or None,
            evidence=evidence,
            provenance="learned",
        )
    else:
        ratio = Ratio(
            parent=parent,
            kind="distribution",
            zero_share=profiled.zero_share or None,
            histogram=[
                HistogramBin(
                    children=b.children, weight=b.weight, low=b.low, high=b.high,
                )
                for b in profiled.histogram
            ],
            evidence=evidence,
            provenance="learned",
        )
    return ratio, profiled.mean


def _stated_ratio(parent: str, override: dict[str, Any]) -> Ratio:
    """A children-per-parent rule a person stated, rather than one measured."""
    kind = override["kind"]
    share = override.get("zero_share")
    detail = (
        f"{override['value']:g} children per parent"
        if kind == "fixed"
        else f"between {int(override['min'])} and {int(override['max'])} children"
    )
    if share:
        detail += f", {float(share):.0%} with none"
    return Ratio(
        parent=parent,
        kind=kind,
        value=float(override["value"]) if kind == "fixed" else None,
        min=int(override["min"]) if kind == "range" else None,
        max=int(override["max"]) if kind == "range" else None,
        zero_share=float(share) if share else None,
        evidence=[Evidence(kind="user", detail=f"set by hand: {detail}", coverage=None)],
        provenance="user",
    )


# Rows for a top-level table that has none and was given no count.
DEFAULT_ROOT_COUNT = 1000


# Raised whenever building a plan from the same inputs gives a different plan,
# so plans already built are rebuilt rather than run as they were.
#   2: a date into a text column is written in that column's form.
#   3: a claim's diagnosis and its lines' procedures are real pairs.
#   4: every group of dependent features is drawn as real combinations.
BUILDER_REVISION = 4


def build_plan(inputs: BuildInputs) -> tuple[Plan, Hierarchy]:
    modes = table_modes(inputs)
    graph = hierarchy_module.build(
        inputs.model, modes, driving_parents=inputs.driving_parents
    )

    # Ratios first: the hierarchy needs the means to compute effective counts.
    ratios: dict[str, Ratio] = {}
    means: dict[str, float] = {}
    stated = table_ratios(inputs)
    for name, node in graph.nodes.items():
        if node.driving_parent is None:
            continue
        ratio, mean = ratio_for(
            inputs.report, name, node.driving_parent, stated.get(name),
        )
        if ratio is not None and mean is not None:
            ratios[name] = ratio
            means[name] = mean

    reference_sizes = {t: rs.size for t, rs in inputs.reference_sets.items()}
    # A top-level table nobody gave a count -- the ordinary case for a database
    # just onboarded -- starts at its own size, or DEFAULT_ROOT_COUNT when it is
    # empty, and the plan says so. Refusing to build left nowhere to state one:
    # the counts are edited on the plan that could not be built.
    root_counts = dict(inputs.root_counts)
    assumed: list[str] = []
    for node in graph.roots():
        if node.name in root_counts:
            continue
        table = inputs.model.table(node.name)
        size = table.row_count if table is not None and table.row_count else DEFAULT_ROOT_COUNT
        root_counts[node.name] = size
        assumed.append(f"{node.name}: {size:,}")
    hierarchy_module.compute_counts(
        graph, root_counts, means, reference_sizes=reference_sizes
    )

    by_column = {p.qualified: p for p in inputs.proposals}
    elements = element_index(inputs.model, inputs.mappings)
    decisions = decisions_index(inputs.decisions)

    tables: list[PlanTable] = []
    for table in inputs.model.tables:
        mode = modes[table.name]
        node = graph.nodes.get(table.name)
        plan_table = PlanTable(
            name=table.name,
            schema_name=table.schema_name,
            table_name=table.table_name,
            mode=mode,
            count=node.count if node else None,
            effective_count=node.effective_count if node else None,
            driving_parent=node.driving_parent if node else None,
            level=node.level if node else None,
            ratio=ratios.get(table.name),
            relationships=_relationships(table) or None,
            source=_reuse_source(table, inputs) if mode == "reuse" else None,
        )

        if mode == "generate":
            for column in table.columns:
                proposal = by_column.get(f"{table.name}.{column.name}")
                if proposal is None:
                    continue
                mapping = inputs.mappings.for_column(table.name, column.name)
                element = mapping.element if mapping and mapping.accepted else None
                decision = decisions.get(f"{table.name}.{column.name}")
                cohort_name = (decision or {}).get("_cohort")
                if cohort_name:
                    # The column keeps its own rule; the decision applies only
                    # to rows inside the cohort, so it must not displace it.
                    resolved = resolve_column(
                        table, column, proposal, element, inputs.catalog,
                        elements.get(table.name, {}), None,
                    )
                    # One rule per cohort that states this column.
                    resolved.overrides = [
                        FieldOverride(
                            cohort=o["cohort"],
                            generator=o["generator"],
                            params=dict(o.get("params") or {}),
                        )
                        for o in (decision.get("_overrides") or [{
                            "cohort": cohort_name, "generator": decision["generator"],
                            "params": decision.get("params")}])
                    ]
                else:
                    if decision and decision.get("approved") and decision.get("element"):
                        element = decision["element"]
                    resolved = resolve_column(
                        table, column, proposal, element, inputs.catalog,
                        elements.get(table.name, {}), decision,
                    )
                plan_table.fields.append(resolved)
            _apply_driving_picks(plan_table)

        tables.append(plan_table)

    promote_referenced_identity_keys(tables, inputs.model, inputs.report)
    _ground_anchors_on_reused_tables(tables, inputs.report)
    _write_text_dates_as_measured(tables, inputs.model, inputs.report)
    _draw_combinations(tables, inputs)

    # Cohorts live beside the decisions that reference them, so a run started
    # in a browser is finishable at a prompt.
    cohorts = [
        Cohort(**{k: v for k, v in entry.items() if k in {"name", "subject", "share", "label", "source"}})
        for entry in (inputs.decisions.get("cohorts") or [])
        if entry.get("name") and entry.get("subject")
    ]
    named = {c.name for c in cohorts}
    for built in tables:
        for plan_field in built.fields:
            if plan_field.overrides:
                kept = [o for o in plan_field.overrides if o.cohort in named]
                plan_field.overrides = kept or None

    plan = Plan(
        meta=new_meta(
            inputs.name,
            inputs.seed,
            builder=BUILDER_REVISION,
            description=(
                f"Resolved from {inputs.model.connection.get('database', 'the source')} "
                f"against {inputs.catalog.organization or 'the catalog'}"
            ),
            catalog=inputs.catalog_path,
            reference_sets=[
                ReferenceSetRecord(**rs.as_record()) for rs in inputs.reference_sets.values()
            ] or None,
        ),
        sources=_sources(inputs),
        cohorts=cohorts,
        tables=tables,
        generation_order=[n for n in graph.order],
        warnings=[],
    )
    plan.warnings = validate(plan)
    if assumed:
        plan.warnings.append(Warning_(
            code="root_count_assumed",
            severity="warn",
            message=("No row count was given for "
                     + "; ".join(assumed)
                     + " -- each starts at its current size, or "
                     f"{DEFAULT_ROOT_COUNT:,} when empty. Change them on the plan."),
        ))
    return plan, graph


def _relationships(table) -> list[Relationship]:
    return [
        Relationship(
            kind=fk.kind,
            name=fk.name,
            columns=list(fk.columns),
            parent_table=fk.parent_table,
            parent_columns=list(fk.parent_columns),
            confidence="high" if fk.kind == "declared" else "high",
            coverage=fk.coverage,
            evidence=[Evidence(kind="value_inclusion", detail=line) for line in fk.evidence]
            or None,
        )
        for fk in table.foreign_keys
    ]


def parent_links(table: PlanTable, model) -> list[tuple[str, list[str], list[str]]]:
    """The tables a table's rows point at: (parent, its columns, the parent's).

    From the plan's own reference rules first -- a claim line's claim key is
    drawn from the claim, whatever the database declares -- then from the
    foreign keys the database declares or profiling inferred.
    """
    links: list[tuple[str, list[str], list[str]]] = []
    for plan_field in table.fields:
        parent = plan_field.params.get("parent_table") if plan_field.generator == "reference" else None
        column = plan_field.params.get("parent_column")
        if parent and column:
            links.append((parent, [plan_field.name], [column]))
    physical = model.table(table.name) if model is not None else None
    for key in (physical.foreign_keys if physical else []):
        if not any(parent == key.parent_table for parent, _c, _p in links):
            links.append((key.parent_table, list(key.columns), list(key.parent_columns)))
    return links


def _draw_combinations(tables: list[PlanTable], inputs: BuildInputs) -> None:
    """Draw each group of dependent features as the combinations the store holds.

    See sdg.plan.combinations. A group is placed wherever its columns sit in one
    table or in a table and the parent its rows belong to; deepest tables first,
    so a claim line and its claim are given the group together -- the claim
    draws first, the line from the combinations agreeing with it.

    Only free columns are taken: one whose rule somebody chose stays theirs, and
    one drawn from the row it belongs to (a key, a value carried down from a
    parent) is not a choice to make. Kept to what the target holds: a column
    pointing into a table draws only that table's keys, a list only its values.
    """
    if not inputs.combinations:
        return
    by_name = {t.name: t for t in tables}
    decided = decisions_index(inputs.decisions)

    def free(table: PlanTable, plan_field) -> bool:
        if plan_field.generator == "skip":
            return False
        if (decided.get(f"{table.name}.{plan_field.name}") or {}).get("generator"):
            return False
        if plan_field.params.get("record_key"):
            return False
        if plan_field.generator == "reference":
            return plan_field.params.get("pick") != "driving"
        return plan_field.generator in {"lookup", "pattern"}

    def allowed(plan_field) -> set[str] | None:
        if plan_field.generator == "reference":
            reference = inputs.reference_sets.get(plan_field.params.get("parent_table") or "")
            return {str(k).strip() for k in reference.keys} if reference else None
        if plan_field.generator == "lookup" and isinstance(plan_field.params.get("values"), list):
            return {str(v).strip() for v in plan_field.params["values"]
                    if not isinstance(v, dict)} or None
        return None

    def integer(table: PlanTable, plan_field) -> bool:
        physical = inputs.model.table(table.name)
        column = physical.column(plan_field.name) if physical else None
        return bool(column and column.is_integer)

    for group in inputs.combinations:
        taken: set[tuple[str, str]] = set()
        for table in sorted(tables, key=lambda t: -(t.level or 0)):
            if table.mode != "generate":
                continue
            # This table's fields of the group, and its parents' -- up to two
            # levels, where a line's claim and the claim's member live.
            found: dict[str, tuple[PlanTable, Any]] = {}
            for plan_field in table.fields:
                if (plan_field.element in group.elements and free(table, plan_field)
                        and (table.name, plan_field.name) not in taken):
                    found.setdefault(plan_field.element, (table, plan_field))
            if not found:
                continue
            anchors: dict[str, tuple[PlanTable, Any]] = {}
            frontier, seen = [table], {table.name}
            # Drawn here: this table and its parents two levels up. Held above
            # and agreed with: four levels up, where a line's subscriber holds
            # the plan.
            for depth in range(4):
                nxt = []
                for child in frontier:
                    for parent_name, _c, _p in parent_links(child, inputs.model):
                        parent = by_name.get(parent_name)
                        if parent is None or parent.mode != "generate" or parent.name in seen:
                            continue
                        seen.add(parent.name)
                        nxt.append(parent)
                        for plan_field in parent.fields:
                            if (depth < 2 and plan_field.element in group.elements
                                    and plan_field.element not in found
                                    and free(parent, plan_field)
                                    and (parent.name, plan_field.name) not in taken):
                                found[plan_field.element] = (parent, plan_field)
                            elif (plan_field.element in group.elements
                                    and plan_field.element not in found
                                    and plan_field.generator != "skip"):
                                # Already decided above this row -- the member's
                                # plan, drawn with its group and product. Not
                                # drawn again: what is drawn here agrees with it.
                                anchors.setdefault(plan_field.element, (parent, plan_field))
                frontier = nxt
            if len(found) < 2:
                continue

            anchors = {e: a for e, a in anchors.items() if e not in found}
            elements = [e for e in group.elements if e in found]
            held = [e for e in group.elements if e in anchors]
            index = [group.elements.index(e) for e in elements + held]
            limits = {e: allowed(found[e][1]) for e in elements}
            counted: dict[tuple[str, ...], int] = {}
            for values, n in group.combinations:
                projected = tuple(values[i] for i in index)
                # An empty value (no network: out of network) points at nothing,
                # so no key set rules it out.
                if all(limits[e] is None or v is None or v in limits[e]
                       for e, v in zip(elements, projected[:len(elements)])):
                    counted[projected] = counted.get(projected, 0) + n
            if not counted:
                continue
            # An empty value in a combination (no network) sorts as blank.
            ranked = sorted(counted.items(),
                            key=lambda kv: (-kv[1], tuple("" if v is None else v for v in kv[0])))
            total = float(sum(n for _, n in ranked)) or 1.0
            typed = {e: integer(*found[e]) for e in elements}
            typed.update({e: integer(*anchors[e]) for e in held})
            records = [
                {e: (int(v) if typed[e] and v is not None and v.lstrip("-").isdigit() else v)
                 for e, v in zip(elements + held, values)}
                for values, _ in ranked
            ]
            weights = [round(n / total, 8) for _, n in ranked]
            key = "combo:" + "+".join(sorted(elements))
            strongest = max((v for a, b, v in group.evidence
                             if a in elements and b in elements), default=None)
            said = Evidence(
                kind="combination",
                detail=(f"drawn with {', '.join(e for e in elements)} as one of "
                        f"{len(records)} combinations the store holds"
                        + (f"; drawn apart, {strongest:.0%} would be combinations it never holds"
                           if strongest else "")),
            )
            for element in elements:
                holder, plan_field = found[element]
                given = {}
                for other in elements + held:
                    if other == element:
                        continue
                    other_table, other_field = found.get(other) or anchors[other]
                    given[other] = (other_field.name if other_table is holder
                                    else f"{other_table.name}.{other_field.name}")
                params: dict[str, Any] = {
                    "values": records, "weights": weights,
                    "record_key": key, "field": element, "given": given,
                }
                within = (plan_field.params.get("parent_table")
                          if plan_field.generator == "reference" else plan_field.params.get("within"))
                if within:
                    params["within"] = within
                plan_field.generator = "lookup"
                plan_field.params = params
                plan_field.evidence = [said, *(plan_field.evidence or [])]
                taken.add((holder.name, plan_field.name))


# A text column's measured shape, and the way a date is written to match it.
_TEXT_DATE_SHAPES = {
    r"\d{8}": "%Y%m%d",
    r"\d{4}-\d{2}-\d{2}": "%Y-%m-%d",
    r"\d{2}/\d{2}/\d{4}": "%m/%d/%Y",
    r"\d{4}/\d{2}/\d{2}": "%Y/%m/%d",
}


def _write_text_dates_as_measured(tables: list[PlanTable], model, report) -> None:
    """Give a date generated into a text column the form that column holds.

    A catalog element such as DOB or SERVICE_DATE makes a date, and a date
    sent to a char(8) column reaches the database as 2025-08-28 -- ten
    characters, refused as right truncation at the first row. The column's
    own values say how it writes a date (20250828), so that is the format; a
    column with no measured shape is given the one its width can hold.
    """
    from sdg.discovery.model import CHARACTER_TYPES

    shapes: dict[str, str] = {}
    for column in getattr(report, "columns", []) or []:
        regex = column.format_regex()
        if regex in _TEXT_DATE_SHAPES:
            shapes[f"{column.table}.{column.column}"] = _TEXT_DATE_SHAPES[regex]

    by_name = {t.name: t for t in model.tables}
    for table in tables:
        physical = by_name.get(table.name)
        if physical is None:
            continue
        for plan_field in table.fields:
            if plan_field.generator != "date_relative" or plan_field.params.get("format"):
                continue
            column = physical.column(plan_field.name)
            if column is None or column.type_name not in CHARACTER_TYPES:
                continue
            width = column.character_length()
            fmt = shapes.get(f"{table.name}.{plan_field.name}")
            if fmt is None:
                fmt = "%Y%m%d" if width is not None and width < 10 else "%Y-%m-%d"
            if plan_field.params.get("with_time") and (width is None or width >= 19):
                fmt += " %H:%M:%S"
            plan_field.params["format"] = fmt


def _ground_anchors_on_reused_tables(tables: list[PlanTable], report) -> None:
    """Replace an anchor that names a table this run does not generate.

    Profiling learns orderings across tables -- a subscriber's effective date
    falls after its group's. When the far table is generated too, the engine
    reaches its row through the cascade and the rule holds. When it is reused,
    there is no row to reach: a reference set carries keys, not the columns a
    date could be measured from, and the run would stop on the first row.

    The ordering is still worth keeping, so the anchor is grounded on the
    earliest value the far column was measured holding. The window is the same;
    it is simply fixed rather than drawn per row.
    """
    generated = {table.name for table in tables if table.mode == "generate"}
    measured: dict[str, Any] = {}
    for column in getattr(report, "columns", []) or []:
        measured[f"{column.table}.{column.column}"] = column.minimum

    for table in tables:
        if table.mode != "generate":
            continue
        for plan_field in table.fields:
            if plan_field.generator != "date_relative":
                continue
            for key in ("anchor", "upper_anchor"):
                reference = plan_field.params.get(key)
                if not isinstance(reference, str) or "." not in reference:
                    continue
                far_table = reference.rpartition(".")[0]
                if far_table in generated or far_table == table.name:
                    continue
                floor = measured.get(reference)
                if floor is None:
                    plan_field.params.pop(key, None)
                    continue
                plan_field.params[key] = str(floor)[:10]


def promote_referenced_identity_keys(
    tables: list[PlanTable], model: PhysicalModel, report: ProfileReport | None
) -> list[str]:
    """Generate identity keys that other generated tables point at.

    An identity column is normally left to the database. That does not work
    when a child in the same run has to reference it: the child needs the key
    while the row is being built, and a key the database has not assigned yet
    cannot be referenced.

    Two ways out. Read the keys back after each insert, or generate them and
    turn on IDENTITY_INSERT for the load. This takes the second, because the
    first makes the keys depend on what the database happened to assign, and a
    run has to be reproducible from its seed.

    Returns the tables that will need IDENTITY_INSERT.
    """
    referenced: set[str] = set()
    generated = {t.name for t in tables if t.mode == "generate"}
    for table in tables:
        if table.mode != "generate":
            continue
        for field_ in table.fields:
            if field_.generator != "reference":
                continue
            parent = field_.params.get("parent_table")
            if parent in generated:
                referenced.add(f"{parent}.{field_.params.get('parent_column')}")

    needs_identity_insert: list[str] = []
    for table in tables:
        if table.mode != "generate":
            continue
        physical = model.table(table.name)
        if physical is None:
            continue
        for field_ in table.fields:
            column = physical.column(field_.name)
            if column is None or not column.is_identity:
                continue
            if f"{table.name}.{field_.name}" not in referenced:
                continue

            start = 1
            detail = "no profile was available, so numbering starts at 1"
            profile = report.column(table.name, field_.name) if report else None
            if profile and profile.sequence and profile.sequence.get("max") is not None:
                start = int(profile.sequence["max"]) + 1
                detail = (
                    f"the highest value already in the table is "
                    f"{profile.sequence['max']:,}, so generated rows start after it"
                )

            field_.generator = "sequence"
            field_.params = {"start": start, "step": 1, "scope": "table"}
            field_.provenance = "structural"
            field_.confidence = "high"
            field_.evidence = [
                Evidence(
                    kind="identity_referenced",
                    detail=(
                        f"an identity column that generated rows in this run reference, "
                        f"so the key is generated here and written with IDENTITY_INSERT; "
                        f"{detail}"
                    ),
                    coverage=1.0,
                )
            ]
            if table.name not in needs_identity_insert:
                needs_identity_insert.append(table.name)
    return needs_identity_insert


def _apply_driving_picks(plan_table: PlanTable) -> None:
    """The driving parent's key is taken from the parent row, not hashed.

    Every other reference is picked from the parent's key set. Getting this
    right is what makes the ratio mean what it says: three claims per member
    means three claims pointing at that member, not three claims pointing
    anywhere.
    """
    if not plan_table.driving_parent:
        return
    for plan_field in plan_table.fields:
        if (
            plan_field.generator == "reference"
            and plan_field.params.get("parent_table") == plan_table.driving_parent
        ):
            plan_field.params = {**plan_field.params, "pick": "driving"}


def _reuse_source(table, inputs: BuildInputs):
    from sdg.plan.model import ReuseSource

    existing = inputs.reference_sets.get(table.name)
    key_columns = table.primary_key or (
        [existing.key_column] if existing else []
    )
    if not key_columns:
        return None
    return ReuseSource(
        connection="source",
        schema_name=table.schema_name,
        table=table.table_name,
        key_columns=key_columns,
        load=existing.strategy if existing else "full",
    )


def _sources(inputs: BuildInputs) -> list[Source]:
    connection = inputs.model.connection
    sources = [
        Source(
            name=connection.get("name", "source"),
            kind="mssql",
            server=connection.get("server"),
            database=connection.get("database"),
            driver=connection.get("driver"),
        )
    ]
    # Reference sets name a connection, so they are compared by connection name.
    seen = {sources[0].name}
    for reference in inputs.reference_sets.values():
        if reference.connection not in seen:
            seen.add(reference.connection)
            sources.append(Source(name=reference.connection, kind="mssql"))
    return sources


def default_reference_sets(
    registry: ConnectionRegistry,
    inputs: BuildInputs,
) -> dict[str, ReferenceSet]:
    """Load a reference set for every reuse table and every reuse element."""
    from sdg.plan.refsets import catalog_reuse_bindings

    bindings: dict[str, dict[str, Any]] = {}
    modes = table_modes(inputs)
    for table in inputs.model.tables:
        if modes[table.name] != "reuse":
            continue
        if len(table.primary_key) != 1:
            continue
        settings = (inputs.decisions.get("tables") or {}).get(table.name) or {}
        bindings[table.name] = {
            "connection": "source",
            "table": table.table_name,
            "schema": table.schema_name,
            "key_column": table.primary_key[0],
            "filter": settings.get("filter"),
            "load": settings.get("load", "full"),
            "sample_size": settings.get("sample_size"),
        }
    bindings.update(catalog_reuse_bindings(inputs.catalog))
    return load_for_plan(registry, bindings, seed=inputs.seed)
