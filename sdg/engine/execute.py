"""Run a plan and produce rows.

The engine walks tables in generation order. Root tables take an absolute
count; child tables walk their driving parent and draw a child count per parent
row from the ratio. Within a row, fields are evaluated in dependency order, not
declaration order, so a plan whose fields are listed in any order produces the
same data.

Two things happen after the main walk. Expressions that aggregate over a child
table are resolved once that table exists, and the run reports what it wrote.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from typing import Any, Iterable

from sdg.engine.expressions import ExpressionError, compile_expression
from sdg.engine.generators import (
    SKIPPED,
    DeferredValue,
    GenerationError,
    RowContext,
    get_generator,
)
from sdg.plan.model import Plan, PlanField, PlanTable, Ratio

# A preview caps root tables. Child tables cascade from them, bounded by this so
# a plan with large ratios cannot generate an unbounded preview.
CASCADE_FACTOR = 200
CASCADE_FLOOR = 2_000


@dataclass
class TableResult:
    name: str
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)
    skipped_fields: list[str] = dataclass_field(default_factory=list)
    # For each parent row index, which of this table's rows belong to it.
    rows_by_parent: dict[int, list[int]] = dataclass_field(default_factory=dict)
    driving_parent: str | None = None
    # How many parent rows were walked. Below this every parent's children are
    # complete; at or above it, none were generated.
    parents_walked: int = 0
    # Cohort membership per row, as a bitmask: one bit per cohort in the plan.
    # A bitmask rather than a set of names because this is read once per field
    # per row -- an integer `and` is free where a set lookup is not -- and
    # because a child inherits its parent's membership by copying one integer.
    # Empty when the plan declares no cohorts, which is the usual case.
    tags: list[int] = dataclass_field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


@dataclass
class RunResult:
    """What one execution produced."""

    tables: dict[str, TableResult] = dataclass_field(default_factory=dict)
    seconds: float = 0.0
    truncated: list[str] = dataclass_field(default_factory=list)

    def rows(self, table: str) -> list[dict[str, Any]]:
        result = self.tables.get(table)
        return result.rows if result else []

    def counts(self) -> dict[str, int]:
        return {name: len(result) for name, result in self.tables.items()}


class ReferenceSets:
    """Keys, and optionally attributes, for tables the plan reuses rather than generates."""

    def __init__(self) -> None:
        self.keys: dict[str, list[Any]] = {}
        self.attributes: dict[str, dict[Any, dict[str, Any]]] = {}

    def add(self, table: str, keys: Iterable[Any], attributes: dict[Any, dict] | None = None) -> None:
        self.keys[table] = list(keys)
        if attributes:
            self.attributes[table] = attributes

    def size(self, table: str) -> int:
        return len(self.keys.get(table, ()))

    def __bool__(self) -> bool:
        return bool(self.keys)


# ---------------------------------------------------------------------------
# Field ordering
# ---------------------------------------------------------------------------


def field_dependencies(table: PlanTable) -> dict[str, set[str]]:
    """Which fields each field must be evaluated after.

    Edges come from anchors and expressions that name a field in the same row,
    and from anchors that name a parent table this row reaches through a
    reference field rather than through its driving parent.
    """
    own = {f.name for f in table.fields}
    # Which reference field brings each non-driving parent table into the row.
    brings_parent: dict[str, str] = {}
    for candidate in table.fields:
        if candidate.generator != "reference":
            continue
        parent = candidate.params.get("parent_table")
        if parent and parent != table.driving_parent:
            brings_parent.setdefault(parent, candidate.name)

    edges: dict[str, set[str]] = {f.name: set() for f in table.fields}

    def note(field_name: str, reference: str) -> None:
        if reference in own:
            edges[field_name].add(reference)
            return
        if "." in reference:
            parent_table = reference.rpartition(".")[0]
            provider = brings_parent.get(parent_table)
            if provider and provider != field_name:
                edges[field_name].add(provider)

    for plan_field in table.fields:
        if plan_field.generator == "date_relative":
            for key in ("anchor", "upper_anchor"):
                anchor = plan_field.params.get(key)
                if isinstance(anchor, str):
                    note(plan_field.name, anchor)
        elif plan_field.generator == "expression":
            try:
                compiled = compile_expression(plan_field.params.get("expression", ""))
            except ExpressionError:
                continue
            for name in compiled.names:
                note(plan_field.name, name)
        elif plan_field.generator == "reference":
            match_field = plan_field.params.get("match_field")
            if isinstance(match_field, str):
                note(plan_field.name, match_field)
    return edges


def ordered_fields(table: PlanTable) -> list[PlanField]:
    """Fields in dependency order. Ties break on name, never on position."""
    edges = field_dependencies(table)
    by_name = {f.name: f for f in table.fields}
    resolved: list[PlanField] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name in done:
            return
        if name in visiting:
            cycle = " -> ".join(trail + (name,))
            raise GenerationError(f"{table.name}: fields depend on each other: {cycle}")
        visiting.add(name)
        for dependency in sorted(edges.get(name, ())):
            visit(dependency, trail + (name,))
        visiting.discard(name)
        done.add(name)
        resolved.append(by_name[name])

    for name in sorted(by_name):
        visit(name, ())
    return resolved


# ---------------------------------------------------------------------------
# Ratios
# ---------------------------------------------------------------------------


def children_for_parent(ratio: Ratio, seed: str, scenario: str, table: str, parent_index: int) -> int:
    """How many child rows this parent row gets. Deterministic in the parent index."""
    from sdg.engine.seed import ValueRandom, value_seed

    rnd = ValueRandom(value_seed(seed, scenario, f"{table}#ratio", parent_index))

    # A histogram already carries its childless parents in the zero bin, so the
    # share is only drawn for the shapes that cannot express it themselves.
    if ratio.kind in ("fixed", "range") and ratio.zero_share:
        if rnd.random() < float(ratio.zero_share):
            return 0

    if ratio.kind == "fixed":
        whole = int(ratio.value)
        remainder = float(ratio.value) - whole
        return whole + (1 if remainder > 0 and rnd.random() < remainder else 0)

    if ratio.kind == "range":
        return rnd.randint(int(ratio.min), int(ratio.max))

    bins = ratio.histogram or []
    index = rnd.weighted_index([b.weight for b in bins])
    chosen = bins[index]
    # A bin that stands for a run of counts is drawn across that run, so a
    # bucketed tail comes out continuous rather than as one spike per bucket.
    if chosen.low is not None and chosen.high is not None and chosen.high > chosen.low:
        return rnd.randint(int(chosen.low), int(chosen.high))
    return int(chosen.children)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class Engine:
    def __init__(
        self,
        plan: Plan,
        *,
        reference_sets: ReferenceSets | None = None,
        scenario: str | None = None,
    ) -> None:
        self.plan = plan
        self.seed = plan.meta.seed
        self.scenario = scenario or plan.meta.name
        self.reference_sets = reference_sets or ReferenceSets()
        self.result = RunResult()
        # One bit per cohort. Everything cohort-related is skipped when the
        # plan declares none, so a plan without patterns pays nothing for this.
        self._cohort_bit = {c.name: 1 << i for i, c in enumerate(plan.cohorts)}
        self._cohorts_by_subject: dict[str, list[tuple[int, float, str]]] = {}
        for cohort in plan.cohorts:
            self._cohorts_by_subject.setdefault(cohort.subject, []).append(
                (self._cohort_bit[cohort.name], float(cohort.share), cohort.name)
            )
        self._tagging = bool(self._cohort_bit)
        # Key values per table, for hash picks, and key -> row index for lookups.
        self._keys: dict[str, list[Any]] = {}
        self._key_index: dict[str, dict[Any, int]] = {}
        self._deferred: list[tuple[str, int, PlanField]] = []
        # Each row's own ancestors, kept only for tables that drive others, so
        # a claim can read its member's subscriber -- the plan it belongs to.
        self._drives = {t.driving_parent for t in plan.tables if t.driving_parent}
        self._ancestry: dict[str, list[dict[str, dict[str, Any]]]] = {}

    # -- public ----------------------------------------------------------

    def run(self, limit: int | None = None) -> RunResult:
        """Generate every table.

        `limit` caps root tables. Child tables follow their parents in full, so
        a preview stays internally consistent: every parent that exists has all
        of its children, and aggregates over them are correct. A ceiling keeps a
        deep plan with large ratios from running away.
        """
        started = time.perf_counter()
        from sdg.engine.generators.lookup import caching

        with caching():
            for table in self.plan.tables_in_order():
                self._generate_table(table, limit)
            self._resolve_deferred()
        self.result.seconds = time.perf_counter() - started
        return self.result

    # -- per table -------------------------------------------------------

    def _generate_table(self, table: PlanTable, limit: int | None) -> None:
        fields = ordered_fields(table)
        skipped = [f.name for f in table.fields if f.generator == "skip"]
        result = TableResult(
            name=table.name, skipped_fields=skipped, driving_parent=table.driving_parent
        )
        self.result.tables[table.name] = result

        key_field = self._key_field(table)

        if table.ratio is None:
            total = table.effective_count if table.effective_count is not None else table.count
            if total is None:
                raise GenerationError(
                    f"{table.name} has neither a ratio nor a row count, so the engine "
                    f"does not know how many rows to make"
                )
            if limit is not None and total > limit:
                total = limit
                self.result.truncated.append(table.name)
            own = self._cohorts_by_subject.get(table.name) if self._tagging else None
            for index in range(total):
                tags = self._draw_tags(table.name, own, index, 0) if self._tagging else 0
                if self._tagging:
                    result.tags.append(tags)
                result.rows.append(
                    self._build_row(table, fields, index, None, None, 0, tags=tags)
                )
            result.parents_walked = len(result.rows)
        else:
            parent_name = table.ratio.parent
            parent_result = self.result.tables.get(parent_name)
            parent_rows = parent_result.rows if parent_result else []
            if not parent_rows and self.reference_sets.size(parent_name):
                parent_rows = [{} for _ in range(self.reference_sets.size(parent_name))]
            ceiling = None if limit is None else max(limit * CASCADE_FACTOR, CASCADE_FLOOR)
            own = self._cohorts_by_subject.get(table.name) if self._tagging else None
            parent_tags = (
                self.result.tables[parent_name].tags
                if self._tagging and parent_name in self.result.tables
                else []
            )
            for parent_index, parent_row in enumerate(parent_rows):
                inherited = (
                    parent_tags[parent_index]
                    if self._tagging and parent_index < len(parent_tags)
                    else 0
                )
                # The cap is checked between parents, never inside one, so every
                # parent that is kept keeps all of its children and aggregates
                # over them (a claim total summing its lines) stay correct.
                if ceiling is not None and len(result.rows) >= ceiling:
                    if table.name not in self.result.truncated:
                        self.result.truncated.append(table.name)
                    break
                result.parents_walked = parent_index + 1
                count = children_for_parent(
                    table.ratio, self.seed, self.scenario, table.name, parent_index
                )
                positions: list[int] = []
                for child_index in range(count):
                    positions.append(len(result.rows))
                    tags = 0
                    if self._tagging:
                        # Inherited from the parent, then any cohort drawn over
                        # this table itself. Inheritance is what makes a member
                        # in a cohort have all of their claims in it too.
                        tags = inherited
                        if own:
                            tags |= self._draw_tags(
                                table.name, own, len(result.rows), child_index
                            )
                        result.tags.append(tags)
                    result.rows.append(
                        self._build_row(
                            table, fields, len(result.rows), parent_name, parent_row, child_index,
                            parent_index=parent_index, tags=tags,
                        )
                    )
                if positions:
                    result.rows_by_parent[parent_index] = positions

        if key_field:
            keys = [row.get(key_field) for row in result.rows]
            self._keys[table.name] = keys
            self._key_index[table.name] = {k: i for i, k in enumerate(keys)}

    def _draw_tags(
        self,
        table: str,
        cohorts: list[tuple[int, float, str]] | None,
        row_index: int,
        _child_index: int,
    ) -> int:
        """Which cohorts this row belongs to, as a bitmask.

        Decided from the seed and the row's position, so the same plan and seed
        put the same rows in the same cohorts on any machine, and nothing has to
        be stored between runs to know it.
        """
        if not cohorts:
            return 0
        from sdg.engine.seed import ValueRandom, value_seed

        if len(cohorts) > 1 and sum(share for _bit, share, _name in cohorts) <= 1:
            # Shares that fit in one population are parts of it: each row is
            # in at most one, so scenarios loaded together never overlap.
            rnd = ValueRandom(value_seed(self.seed, self.scenario, f"cohorts:{table}", row_index))
            draw, start = rnd.random(), 0.0
            for bit, share, _name in cohorts:
                if start <= draw < start + share:
                    return bit
                start += share
            return 0
        tags = 0
        for bit, share, name in cohorts:
            rnd = ValueRandom(value_seed(self.seed, self.scenario, f"cohort:{name}", row_index))
            if rnd.random() < share:
                tags |= bit
        return tags

    def _key_field(self, table: PlanTable) -> str | None:
        """The column other tables point at. Taken from what references this table."""
        for other in self.plan.tables:
            for plan_field in other.fields:
                if (
                    plan_field.generator == "reference"
                    and plan_field.params.get("parent_table") == table.name
                ):
                    return plan_field.params.get("parent_column")
        return None

    # -- per row ---------------------------------------------------------

    def _build_row(
        self,
        table: PlanTable,
        fields: list[PlanField],
        row_index: int,
        parent_name: str | None,
        parent_row: dict[str, Any] | None,
        child_index: int,
        parent_index: int = 0,
        tags: int = 0,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        parents: dict[str, dict[str, Any]] = {}
        parent_indices: dict[str, int] = {}
        if parent_name and parent_row is not None:
            above = self._ancestry.get(parent_name)
            if above and parent_index < len(above):
                parents.update(above[parent_index])
            parents[parent_name] = parent_row
            parent_indices[parent_name] = parent_index

        for plan_field in fields:
            ctx = RowContext(
                seed=self.seed,
                scenario=self.scenario,
                table=table.name,
                field_name=plan_field.name,
                params=plan_field.params,
                row_index=row_index,
                row=row,
                parents=parents,
                parent_indices=parent_indices,
                parent_keys=self._available_keys(),
                parent_attributes=self.reference_sets.attributes,
                child_index=child_index,
                driving_parent=table.driving_parent,
            )

            generator = plan_field.generator
            # A cohort override replaces the rule for the rows inside it, and
            # only those. The guard is two integer tests when the plan has
            # cohorts and one attribute read when it does not.
            if tags and plan_field.overrides:
                for override in plan_field.overrides:
                    bit = self._cohort_bit.get(override.cohort)
                    if bit and tags & bit:
                        generator = override.generator
                        ctx.params = override.params
                        break

            if generator == "skip":
                continue

            if plan_field.null_rate and ctx.stream("null").random() < plan_field.null_rate:
                row[plan_field.name] = None
                continue

            placeholder = _placeholder(ctx)
            if placeholder is not None:
                row[plan_field.name] = placeholder
                continue

            if (generator == "reference" and plan_field.nullable
                    and not ctx.params.get("keys")
                    and not ctx.parent_keys.get(ctx.params.get("parent_table"))):
                # A nullable link to a table that holds nothing -- a database
                # with no networks set up yet -- is left empty, not a failure.
                row[plan_field.name] = None
                continue

            value = get_generator(generator)(ctx)
            if value is SKIPPED:
                continue
            if isinstance(value, DeferredValue):
                self._deferred.append((table.name, row_index, plan_field))
            row[plan_field.name] = value

            if generator == "reference":
                self._attach_parent(plan_field, value, parents, parent_indices)

        if table.name in self._drives:
            self._ancestry.setdefault(table.name, []).append(parents)
        return row

    def _attach_parent(
        self,
        plan_field: PlanField,
        value: Any,
        parents: dict[str, dict[str, Any]],
        parent_indices: dict[str, int],
    ) -> None:
        """Make a hash-picked parent's row readable by later fields in this row."""
        parent_table = plan_field.params.get("parent_table")
        if not parent_table or parent_table in parents:
            return
        parent_result = self.result.tables.get(parent_table)
        if parent_result is None:
            return
        index = self._key_index.get(parent_table, {}).get(value)
        if index is not None and index < len(parent_result.rows):
            parents[parent_table] = parent_result.rows[index]
            parent_indices[parent_table] = index

    def _available_keys(self) -> dict[str, list[Any]]:
        keys = dict(self.reference_sets.keys)
        keys.update(self._keys)
        return keys

    # -- deferred expressions ---------------------------------------------

    def _resolve_deferred(self) -> None:
        if not self._deferred:
            return
        # Deepest tables first, so an aggregate over an aggregate still works.
        order = {name: i for i, name in enumerate(self.plan.generation_order)}
        for table_name, row_index, plan_field in sorted(
            self._deferred, key=lambda item: -order.get(item[0], 0)
        ):
            rows = self.result.tables[table_name].rows
            if row_index >= len(rows):
                continue
            row = rows[row_index]
            compiled = compile_expression(plan_field.params["expression"])

            def resolve(name: str, _row=row, _table=table_name) -> Any:
                if name in _row:
                    return _row[name]
                raise GenerationError(
                    f"{_table}.{plan_field.name}: expression names {name!r}, which the "
                    f"row does not hold"
                )

            def aggregate(function: str, target: str, _index=row_index, _table=table_name) -> Any:
                return self._aggregate(function, target, _table, _index)

            try:
                value = compiled.evaluate(resolve, aggregate)
            except ExpressionError as exc:
                raise GenerationError(f"{table_name}.{plan_field.name}: {exc}") from exc
            row[plan_field.name] = _round_value(value, plan_field.params.get("decimals"))

    def _aggregate(self, function: str, target: str, parent_table: str, parent_index: int) -> Any:
        child_table, _, column = target.rpartition(".")
        child = self.result.tables.get(child_table)
        if child is None:
            raise GenerationError(
                f"{parent_table}: {function}({target}) names a table the plan does not generate"
            )
        if child.driving_parent != parent_table:
            raise GenerationError(
                f"{parent_table}: {function}({target}) needs {child_table} to be driven by "
                f"{parent_table}, but it is driven by {child.driving_parent}"
            )
        if parent_index >= child.parents_walked:
            # This parent's children were never generated, which happens in a
            # preview. Report the aggregate as unknown rather than as zero.
            return None
        positions = child.rows_by_parent.get(parent_index, [])
        values = [
            child.rows[p].get(column)
            for p in positions
            if child.rows[p].get(column) is not None
        ]
        if function == "count":
            return len(positions)
        if not values:
            return Decimal("0") if function == "sum" else None
        if function == "sum":
            return sum(values[1:], values[0])
        if function == "avg":
            return sum(values[1:], values[0]) / len(values)
        if function == "min_of":
            return min(values)
        if function == "max_of":
            return max(values)
        raise GenerationError(f"unknown aggregate {function!r}")


def _placeholder(ctx) -> Any:
    """A placeholder date the column holds in a share of its filled rows --
    9999-12-31 for "no end" -- or None for a row that gets a real value."""
    found = ctx.params.get("placeholders") if ctx.params else None
    if not found:
        return None
    from datetime import date

    draw = ctx.stream("placeholder").random()
    for placeholder in found:
        draw -= float(placeholder.get("share") or 0)
        if draw < 0:
            return date.fromisoformat(str(placeholder["value"])[:10])
    return None


def _round_value(value: Any, decimals: Any) -> Any:
    if decimals is None or value is None or isinstance(value, str):
        return value
    places = int(decimals)
    if places == 0:
        return int(round(float(value)))
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places))


class NeverGenerated(ValueError):
    """A plan asks to generate a table the business pack never generates."""


def refuse_never_generated(plan: Plan) -> None:
    """Refuse a plan that would generate a table the business pack keeps as
    the source holds it (health's providers). The plan may hold Generate for
    one, so its shape can be modeled and a driving parent chosen; no run that
    makes rows does it. Called by each surface that makes rows from a plan
    the pipeline built (a load, a dry run, files from the plan, the command
    line), not by run_plan: a hand-written plan such as the example's may
    generate any table it describes."""
    from sdg.packs import held

    never = set(held("pack:tables.never_generated") or [])
    asked = sorted(t.name for t in plan.tables if t.mode == "generate" and t.name in never)
    if asked:
        raise NeverGenerated(
            f"{', '.join(asked)} {'is' if len(asked) == 1 else 'are'} never generated: the "
            "business pack keeps it as the source holds it. Set it to Reuse or Exclude, "
            "then run again. Nothing was generated.")


def run_plan(
    plan: Plan,
    *,
    limit: int | None = None,
    reference_sets: ReferenceSets | None = None,
    scenario: str | None = None,
) -> RunResult:
    """Execute a plan and return the rows it produced."""
    return Engine(plan, reference_sets=reference_sets, scenario=scenario).run(limit=limit)
