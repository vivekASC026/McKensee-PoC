"""Typed model for the generation plan.

The plan is the contract. This module is the only place that loads, validates,
and saves it. Validation runs twice: the JSON Schema checks the document shape,
and the model checks the things a schema cannot express, such as whether a
reference field points at a table the plan actually contains.

The canonical form omits keys whose value is null, so saving a loaded plan
reproduces the file it came from.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sdg.plan.filters import parse_filter

SCHEMA_PATH = Path(__file__).with_name("schema.json")

Bucket = Literal["system", "reference", "code", "derivable", "business_key", "free"]
GeneratorName = Literal[
    "sequence", "pattern", "lookup", "number", "date_relative",
    "reference", "expression", "constant", "skip", "street_address", "template",
]
Confidence = Literal["high", "medium", "low"]
Provenance = Literal["structural", "catalog", "learned", "user", "physical"]
TableMode = Literal["generate", "reuse", "exclude"]
Severity = Literal["block", "warn"]

# Params each generator cannot run without. Checked by the model so a plan that
# passes validation is one the engine can execute.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "sequence": ("start",),
    "pattern": ("regex",),
    "lookup": ("values",),
    "number": ("distribution",),
    "date_relative": ("anchor",),
    "reference": ("parent_table", "parent_column"),
    "expression": ("expression",),
    "constant": ("value",),
    "skip": (),
    "street_address": ("streets",),
    "template": ("template",),
}


class PlanError(ValueError):
    """A plan that cannot be loaded or does not hold together."""


@lru_cache(maxsize=1)
def plan_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_document(document: dict[str, Any]) -> None:
    """Check a raw plan document against the JSON Schema."""
    validator = jsonschema.Draft202012Validator(plan_schema())
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [
            f"  {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:10]
        ]
        more = "" if len(errors) <= 10 else f"\n  ... and {len(errors) - 10} more"
        raise PlanError("plan does not match the schema:\n" + "\n".join(lines) + more)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Evidence(Base):
    kind: str
    detail: str = ""
    score: float | None = None
    coverage: float | None = None


class Source(Base):
    name: str
    kind: Literal["mssql", "file"]
    server: str | None = None
    database: str | None = None
    driver: str | None = None
    path: str | None = None


class ReferenceSetRecord(Base):
    table: str
    strategy: Literal["full", "sample", "snapshot"]
    size: int
    sample_seed: str | None = None
    filter: str | None = None
    source: str | None = None


class Meta(Base):
    name: str
    version: str
    seed: str
    created: str
    description: str | None = None
    catalog: str | None = None
    reference_sets: list[ReferenceSetRecord] | None = None
    # Which revision of the builder made it. A plan from an earlier one is
    # rebuilt, so a fix to how plans are made reaches a plan already on disk.
    builder: int | None = None

    @field_validator("created")
    @classmethod
    def _created_is_a_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"created must be an ISO timestamp: {value!r}") from exc
        return value


class ReuseSource(Base):
    connection: str
    table: str
    key_columns: list[str]
    schema_name: str | None = None
    attributes: list[str] | None = None
    filter: str | None = None
    load: Literal["full", "sample", "snapshot"] | None = None
    sample_size: int | None = None
    column_map: dict[str, str] | None = None

    @field_validator("filter")
    @classmethod
    def _filter_is_conditions(cls, value: str | None) -> str | None:
        if value:
            parse_filter(value)  # refused here, when the file is read, not mid-run
        return value


class HistogramBin(Base):
    children: int
    weight: float
    # The inclusive range this bin stands for. Set only when the bin had to
    # cover several counts, so the engine can spread across it instead of
    # emitting one spike where the real data had a run of values.
    low: int | None = None
    high: int | None = None

    def expected(self) -> float:
        """Average children this bin contributes, matching how it is drawn."""
        if self.low is not None and self.high is not None and self.high > self.low:
            return (self.low + self.high) / 2
        return float(self.children)


class Ratio(Base):
    parent: str
    kind: Literal["fixed", "range", "distribution"]
    value: float | None = None
    min: int | None = None
    max: int | None = None
    zero_share: float | None = None
    histogram: list[HistogramBin] | None = None
    evidence: list[Evidence] | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _kind_has_its_parameters(self) -> "Ratio":
        if self.kind == "fixed" and self.value is None:
            raise ValueError("a fixed ratio needs a value")
        if self.kind == "range" and (self.min is None or self.max is None):
            raise ValueError("a range ratio needs min and max")
        if self.kind == "range" and self.min > self.max:
            raise ValueError(f"ratio min {self.min} is above max {self.max}")
        if self.kind == "distribution" and not self.histogram:
            raise ValueError("a distribution ratio needs a histogram")
        if self.histogram and sum(b.weight for b in self.histogram) <= 0:
            raise ValueError("histogram weights sum to zero")
        return self

    def mean_children(self) -> float:
        """Average children per parent, used to compute effective counts.

        The childless share pulls the average down for the shapes that draw it
        separately. A histogram counts its own childless parents in the zero
        bin, so applying the share there would subtract them twice.
        """
        if self.kind == "fixed":
            return float(self.value or 0) * (1 - float(self.zero_share or 0))
        if self.kind == "range":
            middle = (float(self.min) + float(self.max)) / 2
            return middle * (1 - float(self.zero_share or 0))
        total = sum(b.weight for b in self.histogram or [])
        if total <= 0:
            return 0.0
        return sum(b.expected() * b.weight for b in self.histogram) / total


class Relationship(Base):
    kind: Literal["declared", "inferred"]
    columns: list[str]
    parent_table: str
    parent_columns: list[str]
    name: str | None = None
    confidence: Confidence | None = None
    coverage: float | None = None
    evidence: list[Evidence] | None = None


class Cohort(Base):
    """A named share of one entity, and the rules that hold inside it.

    A pattern found in real data describes part of a population, not all of it:
    three per cent of members, not every member. Without this a pattern can only
    be applied to the whole run, which generates a population the analyzer never
    saw.

    `subject` names the table the share is drawn over. Membership is decided once
    per subject row, deterministically from the seed, and the tables below it
    inherit it -- so a member in the cohort has all of their claims in it too,
    which is what makes a cross-table pattern hold together.
    """

    name: str
    subject: str
    share: float = Field(gt=0, le=1)
    label: str | None = None
    source: str | None = None


class FieldOverride(Base):
    """A rule that applies only to rows inside one cohort."""

    cohort: str
    generator: GeneratorName
    params: dict[str, Any] = Field(default_factory=dict)


class OverriddenRule(Base):
    provenance: Provenance
    generator: GeneratorName
    reason: str | None = None


class PlanField(Base):
    name: str
    bucket: Bucket
    generator: GeneratorName
    params: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    element: str | None = None
    confidence: Confidence | None = None
    evidence: list[Evidence] | None = None
    coverage: float | None = None
    user_override: bool = False
    overrode: list[OverriddenRule] | None = None
    data_type: str | None = None
    nullable: bool = False
    null_rate: float | None = None
    # Rules for rows inside a cohort. The field's own generator is what every
    # other row gets, which is the point: a pattern states part of a population.
    overrides: list[FieldOverride] | None = None

    @model_validator(mode="after")
    def _generator_has_its_parameters(self) -> "PlanField":
        missing = [p for p in REQUIRED_PARAMS[self.generator] if p not in self.params]
        if missing:
            raise ValueError(
                f"field {self.name}: generator {self.generator} needs {', '.join(missing)}"
            )
        if self.generator == "lookup":
            values = self.params.get("values") or []
            weights = self.params.get("weights")
            if weights is not None and len(weights) != len(values):
                raise ValueError(
                    f"field {self.name}: lookup has {len(values)} values "
                    f"but {len(weights)} weights"
                )
        return self


class PlanTable(Base):
    name: str
    mode: TableMode
    fields: list[PlanField] = Field(default_factory=list)
    schema_name: str | None = None
    table_name: str | None = None
    source: ReuseSource | None = None
    count: int | None = None
    ratio: Ratio | None = None
    effective_count: int | None = None
    driving_parent: str | None = None
    level: int | None = None
    relationships: list[Relationship] | None = None

    @model_validator(mode="after")
    def _field_names_are_unique(self) -> "PlanTable":
        seen: set[str] = set()
        for field in self.fields:
            if field.name in seen:
                raise ValueError(f"table {self.name}: duplicate field {field.name}")
            seen.add(field.name)
        return self

    def field(self, name: str) -> PlanField | None:
        return next((f for f in self.fields if f.name == name), None)


class Warning_(Base):
    code: str
    severity: Severity
    message: str
    table: str | None = None
    field: str | None = None


class Plan(Base):
    meta: Meta
    sources: list[Source] = Field(default_factory=list)
    cohorts: list[Cohort] = Field(default_factory=list)
    tables: list[PlanTable] = Field(default_factory=list)
    generation_order: list[str] = Field(default_factory=list)
    warnings: list[Warning_] = Field(default_factory=list)

    # -- consistency the JSON Schema cannot express -----------------------

    @model_validator(mode="after")
    def _plan_holds_together(self) -> "Plan":
        names = [t.name for t in self.tables]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate tables in plan: {sorted(duplicates)}")
        known = set(names)

        unknown_order = [n for n in self.generation_order if n not in known]
        if unknown_order:
            raise ValueError(f"generation_order names unknown tables: {unknown_order}")

        generate = {t.name for t in self.tables if t.mode == "generate"}
        missing_order = generate - set(self.generation_order)
        if self.generation_order and missing_order:
            raise ValueError(
                f"generation_order is missing tables set to generate: {sorted(missing_order)}"
            )

        declared = {cohort.name for cohort in self.cohorts}
        duplicate_cohorts = [
            c.name for c in self.cohorts if [x.name for x in self.cohorts].count(c.name) > 1
        ]
        if duplicate_cohorts:
            raise ValueError(f"duplicate cohorts in plan: {sorted(set(duplicate_cohorts))}")
        for cohort in self.cohorts:
            if cohort.subject not in known:
                raise ValueError(
                    f"cohort {cohort.name}: subject names unknown table {cohort.subject}"
                )
        for table in self.tables:
            for field in table.fields:
                for override in field.overrides or []:
                    if override.cohort not in declared:
                        raise ValueError(
                            f"{table.name}.{field.name}: override names unknown cohort "
                            f"{override.cohort}"
                        )

        position = {name: i for i, name in enumerate(self.generation_order)}
        for table in self.tables:
            if table.mode == "reuse" and table.source is None:
                raise ValueError(f"table {table.name} is set to reuse but has no source")
            if table.ratio and table.ratio.parent not in known:
                raise ValueError(
                    f"table {table.name}: ratio names unknown parent {table.ratio.parent}"
                )
            for relationship in table.relationships or []:
                if relationship.parent_table not in known:
                    raise ValueError(
                        f"table {table.name}: relationship names unknown parent "
                        f"{relationship.parent_table}"
                    )
            for field in table.fields:
                if field.generator != "reference":
                    continue
                parent = field.params.get("parent_table")
                if parent not in known:
                    raise ValueError(
                        f"{table.name}.{field.name}: reference names unknown table {parent}"
                    )
                # A parent must be written before the child that points at it.
                if (
                    table.mode == "generate"
                    and table.name in position
                    and parent in position
                    and position[parent] > position[table.name]
                ):
                    raise ValueError(
                        f"{table.name}.{field.name}: parent {parent} is generated "
                        f"after its child"
                    )
        return self

    # -- accessors ---------------------------------------------------------

    def table(self, name: str) -> PlanTable | None:
        return next((t for t in self.tables if t.name == name), None)

    def tables_in_order(self) -> Iterator[PlanTable]:
        for name in self.generation_order:
            table = self.table(name)
            if table is not None and table.mode == "generate":
                yield table

    def blocking_warnings(self) -> list[Warning_]:
        return [w for w in self.warnings if w.severity == "block"]

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Canonical document: no null values, ready to write.

        A plan with no cohorts does not carry the key. Most plans have none, and
        emitting an empty list would rewrite every plan written before cohorts
        existed without saying anything.
        """
        document = self.model_dump(mode="json", exclude_none=True)
        if not document.get("cohorts"):
            document.pop("cohorts", None)
        return document

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = self.to_dict()
        validate_document(document)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "Plan":
        validate_document(document)
        try:
            return cls.model_validate(document)
        except Exception as exc:
            raise PlanError(str(exc)) from exc

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        path = Path(path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PlanError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(document)


def points_at(params: dict[str, Any], generator: str) -> str | None:
    """The table a field's values must be keys of, if any.

    A reference points into its parent table; a combination drawn within a
    table -- a diagnosis drawn with its procedure, kept to the diagnosis table
    -- points into that table just as surely.
    """
    if generator == "reference":
        return params.get("parent_table")
    if generator == "lookup":
        return params.get("within")
    return None


def new_meta(name: str, seed: str, *, version: str = "0.1.0", **extra: Any) -> Meta:
    """Meta block with the created timestamp filled in."""
    return Meta(
        name=name,
        version=version,
        seed=seed,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    )
