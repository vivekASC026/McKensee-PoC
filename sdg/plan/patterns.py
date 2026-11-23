"""Resolve an analyzer pattern onto the fields it governs.

The pattern analyzer finds combinations of business values that occur together
-- a member aged 24 whose claim lines carry procedure code A0000 -- and hands
them over as a list of labeled parameters. This module decides which database
column each label is about and turns its value into a rule the engine already
knows how to run. Where a label maps, the pattern supplies the value; every
other column keeps the rule the tool worked out.

`pattern` here means one of those analyzer findings. It is not the regular
expression the `pattern` generator uses. The collision is unfortunate, but the
word belongs to the analyzer, so this module writes `analyzer pattern` wherever
the two could be confused.

Three things already in the tool make the mapping possible without inventing
new vocabulary:

    the element    mappings.json binds every column to a business element, with
                   evidence. A parameter label resolves to an element, and the
                   element names the columns.
    the subject    a pattern is about a member, or a claim. The kind names an
                   anchor element, the anchor element names the table that
                   carries it as a business key, and that table is the subject.
                   A pattern may only reach the subject and the tables below it.
    the bucket     discovery already classified every column. A column the
                   database fills, computes, or uses as a key is not something a
                   pattern may state, and the planned generator says which is
                   which.

What comes out is a list of targets -- each an ordinary generator and its
parameters against one column -- and a list of rejections saying what could not
be bound and why. Nothing is dropped in silence: a label that does not resolve
is reported with the elements it came closest to.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from sdg.catalog.model import Catalog
from sdg.discovery.classify import tokenize
from sdg.discovery.model import PhysicalModel
from sdg.mapping.scorer import ColumnMapping, MappingStore
from sdg.plan.editing import check, check_fits
from sdg.plan.model import Plan, PlanField, PlanTable
from sdg.profiling.format import matches_format
from sdg import packs
from sdg.packs import held, held_or

# A pattern kind names the entity the pattern is about. The physical table is
# not named here; the anchor element is. The subject is whichever table carries
# that element as its business key, so this registry survives a schema whose
# tables are called something else entirely.
KIND_ANCHORS: dict[str, str] = held("pack:plan.kind_anchors")

# Generators a pattern value may replace. The planned generator decides this,
# not the bucket alone: a column in the `code` bucket can still be a reference
# into a reused table, and restricting which rows that reference may pick is a
# different mechanism from stating a value.
REPLACEABLE: frozenset[str] = frozenset(
    {"pattern", "lookup", "number", "date_relative", "constant"}
)

REFUSALS: dict[str, str] = {
    "expression": (
        "the column is calculated from other columns, so stating it directly would "
        "contradict the calculation; constrain its inputs instead"
    ),
    "sequence": "the column counts upward, so it cannot hold a stated value",
    "reference": (
        "the column points at a real row in another table, and this parameter "
        "states no values to narrow that to"
    ),
    "skip": "the database fills this column itself",
}

# Words that carry no meaning in a parameter label.
STOP_TOKENS: frozenset[str] = frozenset(
    {"OF", "THE", "A", "AN", "FOR", "IN", "ON", "AT", "TO", "BY", "AND", "WITH", "IS"}
)

# Analyzer labels are prose; element names and aliases are database shorthand.
# Both sides are canonicalized through this table and then compared as token
# sets, which is what lets "Proc Code" reach PROCEDURE_CODE when neither the
# element name nor any of its aliases is spelled that way.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "ADDR": ("ADDRESS",),
    "AMT": ("AMOUNT",),
    "BILLED": ("CHARGE",),
    "CD": ("CODE",),
    "CDML": ("LINE",),
    "CHG": ("CHARGE",),
    "CK": ("KEY",),
    "CLCL": ("CLAIM",),
    "CLM": ("CLAIM",),
    "CPT": ("PROCEDURE", "CODE"),
    "DAYS": ("DAY",),
    "DOB": ("BIRTH", "DATE"),
    "DT": ("DATE",),
    "EFF": ("EFFECTIVE",),
    "FAMILY": ("LAST",),
    "FNAME": ("FIRST", "NAME"),
    "GIVEN": ("FIRST",),
    "HCPCS": ("PROCEDURE", "CODE"),
    "LNAME": ("LAST", "NAME"),
    "MBR": ("MEMBER",),
    "MEME": ("MEMBER",),
    "NBR": ("NUMBER",),
    "NO": ("NUMBER",),
    "NUM": ("NUMBER",),
    "PDPD": ("PRODUCT",),
    "POSTAL": ("ZIP",),
    "PROC": ("PROCEDURE",),
    "PROD": ("PRODUCT",),
    "PROV": ("PROVIDER",),
    "PRPR": ("PROVIDER",),
    "QTY": ("UNITS",),
    "RECD": ("RECEIVED",),
    "REL": ("RELATIONSHIP",),
    "SBSB": ("SUBSCRIBER",),
    "SEQ": ("SEQUENCE",),
    "SPCL": ("SPECIALTY",),
    "ST": ("STATE",),
    "STS": ("STATUS",),
    "SUB": ("SUBSCRIBER",),
    "SUBSC": ("SUBSCRIBER",),
    "SURNAME": ("LAST", "NAME"),
    "SVC": ("SERVICE",),
    "TERM": ("TERMINATION",),
    "TOT": ("TOTAL",),
    "TS": ("TIMESTAMP",),
    "YEARS": ("YEAR",),
    "YRS": ("YEAR",),
}

# Parameters that are not columns at all. Age is the archetype: nothing stores
# it, because it moves. Each derivation says which element actually holds the
# value and how to express the parameter as a rule on it.
DERIVATIONS: dict[str, dict[str, Any]] = {
    "age": {
        "label": "Age",
        "requires": frozenset({"AGE"}),
        "requires_any": frozenset(),
        "element": "DOB",
        "why": "age is not stored; it is the gap between a birth date and a reference date",
    },
    "tenure": {
        "label": "Tenure",
        "requires": frozenset({"TENURE"}),
        "requires_any": frozenset(),
        # The date a record began (pack.json plan.tenure_element: health's
        # coverage start, a bank's customer-since date; None where the pack
        # has none, and then nothing is read as tenure).
        "element": held_or("pack:plan.tenure_element", None),
        "why": "tenure is the gap between the date a record began and a reference date",
    },
    "days_to_pay": {
        "label": "Days to pay",
        "requires": frozenset({"DAY"}),
        "requires_any": frozenset({"PAY", "PAID", "PAYMENT"}),
        "element": "PAID_DATE",
        "anchor_element": "RECEIVED_DATE",
        "why": "a turnaround in days is a gap between two stored dates",
    },
}


class PatternError(ValueError):
    """An analyzer document that cannot be read, or a value that cannot be used."""


class _Tolerant(BaseModel):
    """Input from another tool. Unknown keys are ignored, not fatal.

    The analyzer is free to grow its output without this refusing to read it;
    what matters is that every field used here is present and typed.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# What the analyzer hands over
# ---------------------------------------------------------------------------


class PatternParameter(_Tolerant):
    label: str
    value: Any = None
    unit: str | None = None
    values: list[Any] | None = None
    weights: list[float] | None = None
    # A band rather than a point. The analyzer measures a cluster, and a
    # cluster spans a range: collapsing it to its mean would generate a
    # population narrower than the one that was found.
    min: Any = None
    max: Any = None
    # Within the band, how the values spread: [low, high, share] slices (the
    # pattern's deciles), so generated values fall as the pattern's do rather
    # than evenly across the band.
    bands: list[list[float]] | None = None
    # Whether every row below the subject carries the value, or only one of
    # them. A co-occurrence finding usually means "at least one".
    apply: Literal["all", "at_least_one"] | None = None
    # Set when `values` are whole records -- addresses -- and this parameter
    # takes one field of them. Every column fed from the same records draws
    # the same record, so a ZIP and its city stay together.
    record_field: str | None = None
    # Which kind of record: addresses, or clinical combinations. Each kind is
    # drawn under its own key, so an address and a diagnosis do not share a draw.
    record_group: str | None = None

    def record_key(self) -> str:
        return f"pattern_{self.record_group or 'address'}"

    def stated(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        if self.record_field and self.values:
            noun = "places" if self.record_group in (None, "address") else "combinations"
            return f"{self.label}: {len(self.values)} {noun}"
        if self.values:
            return f"{self.label}: {', '.join(str(v) for v in self.values)}"
        if self.min is not None and self.max is not None:
            return f"{self.label}: {self.min} to {self.max}{unit}"
        return f"{self.label}: {self.value}{unit}"


class AnalyzerPattern(_Tolerant):
    id: str
    kind: str
    label: str = ""
    support: float | None = None
    as_of: str | None = None
    subject: str | None = None  # escape hatch: name the table outright
    parameters: list[PatternParameter] = Field(default_factory=list)


class PatternDocument(_Tolerant):
    version: str = "1.0"
    analyzer: str = ""
    as_of: str | None = None
    patterns: list[AnalyzerPattern] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# What resolution produces
# ---------------------------------------------------------------------------


class Target(_Strict):
    """One column a pattern governs, and the rule that will produce its value."""

    table: str
    column: str
    element: str | None = None
    generator: str
    params: dict[str, Any] = Field(default_factory=dict)
    parameter: str  # the analyzer label and value this came from
    relation: Literal["subject", "descendant", "ancestor"]
    depth: int = 0  # levels below the subject (above it, for an ancestor)
    apply: Literal["all", "at_least_one"] = "all"
    confidence: str | None = None  # the mapping's confidence in the element
    replaces: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


class Rejection(_Strict):
    parameter: str
    reason: str
    candidates: list[str] = Field(default_factory=list)


class Resolution(_Strict):
    pattern: str
    kind: str
    label: str = ""
    subject: str | None = None
    support: float | None = None
    as_of: str | None = None
    # Where the pattern's share is drawn: the subject, or the table above it
    # that holds the plan its members are enrolled in (see _enrolled_above).
    share_of: str | None = None
    targets: list[Target] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)

    def bound(self) -> int:
        """How many analyzer parameters found a home."""
        return len({t.parameter for t in self.targets})

    def is_usable(self) -> bool:
        return bool(self.targets)


# ---------------------------------------------------------------------------
# Labels to elements
# ---------------------------------------------------------------------------


def canonical_tokens(text: str) -> frozenset[str]:
    """A label reduced to the tokens that carry meaning, spelled one way."""
    out: list[str] = []
    for token in tokenize(text):
        if token in STOP_TOKENS:
            continue
        out.extend(SYNONYMS.get(token, (token,)))
    return frozenset(out)


def element_keys(catalog: Catalog) -> dict[frozenset[str], set[str]]:
    """Canonical token set -> the elements that answer to it.

    Built from element names and every alias, so one element is reachable by
    several spellings, and a set that reaches two elements is reported as
    ambiguous rather than resolved to whichever came first.
    """
    index: dict[frozenset[str], set[str]] = {}
    for element in catalog.elements:
        for name in element.all_names():
            key = canonical_tokens(name)
            if key:
                index.setdefault(key, set()).add(element.name)
    return index


def _near_elements(key: frozenset[str], index: dict[frozenset[str], set[str]]) -> list[str]:
    """Elements whose tokens overlap the label, best first. For the report."""
    best: dict[str, float] = {}
    for candidate, names in index.items():
        shared = key & candidate
        if not shared:
            continue
        score = len(shared) / len(key | candidate)
        for name in names:
            best[name] = max(best.get(name, 0.0), score)
    return [name for name, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))][:4]


def match_element(
    label: str, index: dict[frozenset[str], set[str]]
) -> tuple[str | None, list[str]]:
    """The element a label names, or nothing and the nearest candidates."""
    key = canonical_tokens(label)
    if not key:
        return None, []
    names = index.get(key)
    if names and len(names) == 1:
        return next(iter(names)), []
    if names:
        return None, sorted(names)
    return None, _near_elements(key, index)


def match_derivation(label: str) -> tuple[str, dict[str, Any]] | None:
    """Whether a label is a derived quantity rather than a stored one."""
    key = canonical_tokens(label)
    for name, spec in DERIVATIONS.items():
        if spec["element"] is None or not spec["requires"] <= key:
            continue
        wanted_any = spec["requires_any"]
        if wanted_any and not (wanted_any & key):
            continue
        return name, spec
    return None


# ---------------------------------------------------------------------------
# The subject, and what sits below it
# ---------------------------------------------------------------------------


def subject_table(kind: str, mappings: MappingStore, plan: Plan) -> tuple[str | None, str]:
    """The table a pattern of this kind is about, and why.

    The kind names an anchor element; the subject is the generated table that
    carries it as a business key. CLAIM_ID also appears on the claim line as a
    foreign key, which is why the bucket matters.
    """
    anchor = KIND_ANCHORS.get(kind.strip().lower())
    if anchor is None:
        return None, (
            f"there is no pattern kind called {kind!r}; known kinds are "
            f"{', '.join(sorted(KIND_ANCHORS))}"
        )
    carriers = [
        m for m in mappings.mappings
        if m.element == anchor and m.bucket == "business_key"
    ]
    generated = [m for m in carriers if _mode(plan, m.table) == "generate"]
    if not generated:
        if carriers:
            return None, (
                f"{anchor} is carried by {carriers[0].table}, which this run does not generate"
            )
        return None, (
            f"no table carries {anchor} as a business key, so there is no {kind} to describe"
        )
    chosen = sorted(generated, key=lambda m: m.table)[0]
    return chosen.table, f"{chosen.table} carries {anchor} as its business key"


def _mode(plan: Plan, table: str) -> str:
    found = plan.table(table)
    return found.mode if found else "unknown"


def ancestry(plan: Plan, table: str) -> list[str]:
    """Tables above this one, nearest first, following driving parents."""
    chain: list[str] = []
    seen: set[str] = set()
    current = plan.table(table)
    while current is not None and current.driving_parent:
        parent = current.driving_parent
        if parent in seen:
            break
        seen.add(parent)
        chain.append(parent)
        current = plan.table(parent)
    return chain


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_pattern(
    pattern: AnalyzerPattern,
    *,
    catalog: Catalog,
    mappings: MappingStore,
    plan: Plan,
    model: PhysicalModel | None = None,
    as_of: str | None = None,
) -> Resolution:
    """Bind every parameter of one analyzer pattern to a column and a rule."""
    index = element_keys(catalog)
    subject, why = subject_table(pattern.kind, mappings, plan)
    if pattern.subject:
        subject, why = pattern.subject, "named by the pattern"

    reference_date = pattern.as_of or as_of
    resolution = Resolution(
        pattern=pattern.id,
        kind=pattern.kind,
        label=pattern.label,
        subject=subject,
        support=pattern.support,
        as_of=reference_date,
    )
    if subject is None:
        resolution.rejections.append(
            Rejection(parameter=f"(the whole pattern, kind {pattern.kind})", reason=why)
        )
        return resolution
    if plan.table(subject) is None:
        resolution.rejections.append(
            Rejection(parameter="(subject)", reason=f"the plan has no table {subject}")
        )
        return resolution

    # Elements the pattern states as part of a record -- a plan with its own
    # product and group. The same element counted on its own is not stated
    # again, or a product would be drawn apart from the plan it belongs to.
    in_records = {match_element(p.label, index)[0] for p in pattern.parameters
                  if p.record_field} - {None}
    # The primary record's table (health's member): the one table above the
    # subject a pattern may state, as a claims pattern's members' ages.
    pack = packs.active()
    primary, _why = subject_table(pack.term(pack.primary, "one"), mappings, plan)
    for parameter in pattern.parameters:
        if not parameter.record_field and match_derivation(parameter.label) is None                 and match_element(parameter.label, index)[0] in in_records:
            resolution.rejections.append(Rejection(
                parameter=parameter.stated(),
                reason="stated with the other fields of its record instead, so they stay paired"))
            continue
        _resolve_parameter(
            parameter,
            resolution=resolution,
            subject=subject,
            mappings=mappings,
            plan=plan,
            model=model,
            index=index,
            as_of=reference_date,
            primary=primary,
        )
    _one_rule_per_column(resolution)
    _keep_records_whole(pattern, resolution)
    _draw_records_given_each_other(resolution)
    return resolution


def _one_rule_per_column(resolution: Resolution) -> None:
    """A column drawn as part of a record keeps that rule.

    Two parameters can land on one column: the claim's provider as part of
    its diagnosis, procedure and network, and the provider's specialty. Only
    one rule can stand, and the record's is the one that keeps the others
    true -- the providers it draws already have the specialties the pattern
    measured. Taking the other would draw a provider no record of the pattern
    holds, and the lines below would have no procedure to agree with it.
    """
    def own(target: Target) -> bool:
        # Drawn from the pattern's own records, not the plan's narrowed.
        return str((target.params or {}).get("record_key") or "").startswith("pattern_")

    in_record = {(t.table, t.column) for t in resolution.targets if own(t)}
    kept = []
    for target in resolution.targets:
        if (target.table, target.column) in in_record and not own(target):
            resolution.rejections.append(Rejection(
                parameter=target.parameter or target.element,
                reason=(f"{target.column} is drawn with the rest of its record, which "
                        f"already holds this"),
            ))
            continue
        kept.append(target)
    resolution.targets = kept


def _draw_records_given_each_other(resolution: Resolution) -> None:
    """Fields of one record in different tables, drawn to agree.

    Columns in one table that take fields of the same records share a draw.
    A diagnosis on the claim and a procedure on its line are in two tables,
    drawn row by row in each, so on their own they would pair at random. Each
    is told where the others are; the engine draws a line's procedure only
    from the combinations holding the diagnosis its claim already has.
    """
    groups: dict[str, list[Target]] = {}
    for target in resolution.targets:
        key = (target.params or {}).get("record_key")
        if key and (target.params or {}).get("field"):
            groups.setdefault(key, []).append(target)
    for members in groups.values():
        if len({t.table for t in members}) < 2:
            continue
        for target in members:
            target.params["given"] = {
                other.params["field"]: (other.column if other.table == target.table
                                        else f"{other.table}.{other.column}")
                for other in members if other is not target
            }


def _keep_records_whole(pattern: AnalyzerPattern, resolution: Resolution) -> None:
    """All of a pattern's addresses, or none of them.

    The fields of one record -- a ZIP, its city, its state -- are drawn
    together. If one of them cannot go into its column because the column is
    too narrow, taking the others alone would write a ZIP with somebody else's
    city. So the whole record goes, and those columns keep the rules they had:
    a target whose types cannot hold the store's values is not filled from it.
    """
    from .editing import TOO_WIDE

    by_label = {p.stated(): p for p in pattern.parameters if p.record_field}
    too_wide = [r for r in resolution.rejections
                if r.parameter in by_label and TOO_WIDE in r.reason]
    if not too_wide:
        return
    # Per kind of record: an address that does not fit says nothing about
    # whether a diagnosis and procedure pair does.
    broken = {by_label[r.parameter].record_key(): r.reason for r in too_wide}
    dropped = [t for t in resolution.targets
               if (t.params or {}).get("record_key") in broken]
    resolution.targets = [t for t in resolution.targets if t not in dropped]
    for target in dropped:
        key = target.params["record_key"]
        whole = ("a ZIP is never written apart from its own city and state"
                 if key == "pattern_address"
                 else "the values of one combination are never written apart")
        resolution.rejections.append(Rejection(
            parameter=target.parameter or target.element,
            reason=(f"{target.column} is left to its own rule: another part of the "
                    f"same record does not fit its column ({broken[key]}), and {whole}"),
        ))


def _resolve_parameter(
    parameter: PatternParameter,
    *,
    resolution: Resolution,
    subject: str,
    mappings: MappingStore,
    plan: Plan,
    model: PhysicalModel | None,
    index: dict[frozenset[str], set[str]],
    as_of: str | None,
    primary: str | None = None,
) -> None:
    stated = parameter.stated()

    def reject(reason: str, candidates: Iterable[str] = ()) -> None:
        resolution.rejections.append(
            Rejection(parameter=stated, reason=reason, candidates=list(candidates))
        )

    # 1. Is this a derived quantity, or one the database stores?
    derivation = match_derivation(parameter.label)
    if derivation is not None:
        _name, spec = derivation
        element = spec["element"]
    else:
        spec = {}
        element, candidates = match_element(parameter.label, index)
        if element is None:
            reject(
                "no element answers to this label"
                + (", and more than one is equally close" if candidates else ""),
                candidates,
            )
            return

    # 2. Which column carries that element, within reach of the subject?
    chosen, note = _pick_column(element, subject=subject, mappings=mappings, plan=plan,
                                primary=primary)
    if chosen is None:
        # An element on a reused table is not out of reach just because the
        # table is not generated. If something the subject writes points at
        # that table, the pattern can narrow which of its rows gets pointed at.
        narrowed = _reference_filter(
            element, parameter, subject=subject, mappings=mappings, plan=plan
        )
        if narrowed is not None:
            resolution.targets.append(narrowed)
            return
        chosen = _enrolled_above(element, parameter, subject=subject,
                                 mappings=mappings, plan=plan)
        if chosen is None:
            reject(note, [element])
            return
        note = (f"{chosen.qualified}: a member's plan and group are its family's, "
                f"so this pattern's share is drawn over {chosen.table.split('.')[-1]}")

    table = plan.table(chosen.table)
    field = table.field(chosen.column) if table else None
    if field is None:
        reject(f"{chosen.qualified} is mapped to {element} but the plan has no such field")
        return

    # 3. May a pattern state this column at all?
    if (parameter.record_group == "enrollment" and chosen.table != subject
            and chosen.table not in ancestry(plan, subject)):
        # Where a member is enrolled is the member's: drawn again on each claim
        # below it, a claim would name a product its member does not have.
        reject(f"{chosen.qualified} is below {subject}; a member's group, plan and "
               f"product are set where the member is, not on its claims", [element])
        return
    refusal = _refuse_field(chosen, field, parameter)
    if refusal:
        reject(refusal, [element])
        return

    # 4. Turn the stated value into a rule.
    try:
        if derivation is not None:
            built = _build_derived(
                spec, parameter, table_name=chosen.table, mappings=mappings, as_of=as_of
            )
        else:
            built = _build_literal(parameter, field=field)
    except PatternError as exc:
        reject(str(exc), [element])
        return
    except (ValueError, TypeError, OverflowError) as exc:
        # Analyzer JSON is loosely typed: a min that is not a number, a short
        # band, an age too large for a date. Reject this parameter, not the
        # whole document.
        reject(f"the stated values could not be read as a rule ({exc})", [element])
        return

    relation: Literal["subject", "descendant", "ancestor"] = (
        "subject" if chosen.table == subject
        else "ancestor" if chosen.table in ancestry(plan, subject)
        else "descendant"
    )
    depth = (0 if relation == "subject"
             else ancestry(plan, subject).index(chosen.table) + 1 if relation == "ancestor"
             else _depth(plan, chosen.table, subject))
    applies = parameter.apply or ("at_least_one" if relation == "descendant" else "all")

    # 5. Columns that are drawn together move together, or not at all.
    group = _shared_group(table, field)
    if group and built["generator"] == "constant":
        expanded = _filter_shared_record(group, field=field, value=built["params"]["value"])
        if expanded is None:
            reject(
                f"no {field.params.get('record_key')} record has "
                f"{field.params.get('field')} = {built['params']['value']!r}, so pinning it "
                f"would break the columns drawn with it",
                [element],
            )
            return
    else:
        expanded = [(field, built["generator"], built["params"], None)]

    # 6. Everything an edit is checked for, a pattern is checked for too. This
    #    is where a value too wide for its column is caught, rather than partway
    #    through a load with rows already committed.
    for sibling, generator, params, _extra in expanded:
        problems = check(generator, params)
        column_meta = None
        if model is not None:
            found = model.table(chosen.table)
            column_meta = found.column(sibling.name) if found else None
        problems += check_fits(generator, params, column_meta)
        if problems:
            reject("; ".join(problems), [element])
            return

    for sibling, generator, params, extra in expanded:
        resolution.targets.append(
            Target(
                table=chosen.table,
                column=sibling.name,
                element=element if sibling is field else sibling.element,
                generator=generator,
                params=params,
                parameter=stated,
                relation=relation,
                depth=depth,
                apply=applies,
                confidence=chosen.confidence,
                replaces={"generator": sibling.generator, "params": sibling.params},
                note=extra or (note or None),
            )
        )
    if relation == "ancestor":
        _share_over(resolution, plan, chosen.table)


def _share_over(resolution: Resolution, plan: Plan, table: str) -> None:
    """Draw the pattern's share over this table, where it is above the one
    the share was drawn over. Rows below inherit their parent's place in a
    share, so the highest table any target is on decides: a member drawn in
    has all of their claims in, and a family drawn in has all of its members."""
    current = resolution.share_of or resolution.subject
    if current is None or table in ancestry(plan, current):
        resolution.share_of = table


def _reference_filter(
    element: str,
    parameter: PatternParameter,
    *,
    subject: str,
    mappings: MappingStore,
    plan: Plan,
) -> "Target | None":
    """Reach an attribute of a reused table by narrowing what points at it.

    PROVIDER_SPECIALTY is a column of the provider table, and the provider
    table is reused -- nothing generates it, so there is no column to state.
    But a claim holds a provider key, and which provider that key picks is
    something the run decides. So "providers in this specialty" becomes a
    restriction on the key: draw only from the providers whose specialty
    column says so.

    This is narrowing, not replacement. The column still points at real rows of
    the real table, and referential integrity is untouched -- there are simply
    fewer rows it may choose from.

    Only a stated set of values can do this. A range or a distribution over an
    attribute of a reference set is a different question, and answering the
    easy half while silently dropping the rest would be worse than refusing.
    """
    if not parameter.values:
        return None

    reused = [
        m for m in mappings.mappings
        if m.element == element and _mode(plan, m.table) == "reuse"
    ]
    if not reused:
        return None

    # The field that points at it has to be one the subject's own rows carry,
    # or one below the subject: a pattern cannot reach back and change a key
    # its parent already holds.
    candidates: list[tuple[int, str, Any, Any]] = []
    for table in plan.tables:
        if table.mode != "generate":
            continue
        if table.name != subject and subject not in ancestry(plan, table.name):
            continue
        for field in table.fields:
            from sdg.plan.model import points_at

            parent = points_at(field.params, field.generator)
            if parent is None:
                continue
            for carrier in reused:
                if parent == carrier.table:
                    level = table.level if table.level is not None else 99
                    candidates.append((level, table.name, field, carrier))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1], item[2].name))
    _level, table_name, field, carrier = candidates[0]

    params = dict(field.params)
    params["filter_attribute"] = carrier.column
    params["filter_values"] = [str(v) for v in parameter.values]
    # A stated filter and a row-matched one would fight over the same key set,
    # and the pattern is the more specific statement.
    params.pop("match_field", None)

    relation: Literal["subject", "descendant"] = (
        "subject" if table_name == subject else "descendant"
    )
    return Target(
        table=table_name,
        column=field.name,
        element=field.element,
        # Narrowed as it is drawn: a reference by its keys, a combination
        # within the table by the combinations holding those keys.
        generator=field.generator,
        params=params,
        parameter=parameter.stated(),
        relation=relation,
        depth=0 if relation == "subject" else _depth(plan, table_name, subject),
        apply=parameter.apply or ("all" if relation == "subject" else "at_least_one"),
        confidence=carrier.confidence,
        replaces={"generator": field.generator, "params": field.params},
        note=(
            f"{element} is on {carrier.qualified}, which is reused; this narrows "
            f"{table_name}.{field.name} to the rows of {carrier.table} whose "
            f"{carrier.column} the pattern named"
        ),
    )


def _pick_column(
    element: str, *, subject: str, mappings: MappingStore, plan: Plan,
    primary: str | None = None,
) -> tuple[ColumnMapping | None, str]:
    """The column a pattern should state for this element.

    An element can be mapped in several places: EFFECTIVE_DATE sits on both the
    subscriber and the member, CHARGE_AMOUNT on both a claim and its lines. The
    subject settles it -- the subject's own column first, then the shallowest
    table below it. Above it, only the primary record's table (`primary`,
    health's member): a claims pattern's members are 44 to 75, said on the
    member, which is generated first, and the pattern's share is drawn over
    members (`_share_over`), so every claim of the pattern has a member of its
    ages. Any other column above the subject is refused rather than guessed:
    a member pattern does not state its subscriber's attributes, which every
    member of the family would then share.
    """
    carriers = [m for m in mappings.mappings if m.element == element]
    if not carriers:
        return None, f"{element} is not mapped to any column"

    on_subject = [m for m in carriers if m.table == subject]
    if on_subject:
        chosen = sorted(on_subject, key=lambda m: m.column)[0]
        # A column the database computes is not a place to state a value, but
        # the value usually exists one level down -- a claim total is the sum of
        # its lines, and the line amount is the thing that was measured. Falling
        # through is better than refusing something the plan can express.
        if chosen.bucket != "derivable":
            return chosen, ""

    reachable: list[tuple[int, str, ColumnMapping]] = []
    above: list[ColumnMapping] = []
    refused: list[str] = []
    elsewhere: list[str] = []
    for carrier in carriers:
        mode = _mode(plan, carrier.table)
        if mode == "reuse":
            elsewhere.append(f"{carrier.qualified} (that table is reused, not generated)")
            continue
        if mode != "generate":
            elsewhere.append(f"{carrier.qualified} (that table is set to {mode})")
            continue
        if subject in ancestry(plan, carrier.table):
            level = plan.table(carrier.table).level
            reachable.append((level if level is not None else 99, carrier.table, carrier))
        elif carrier.table in ancestry(plan, subject):
            if carrier.table == primary and carrier.bucket != "business_key":
                above.append(carrier)
            else:
                refused.append(carrier.qualified)
        else:
            elsewhere.append(f"{carrier.qualified} (not below the subject)")

    if reachable:
        reachable.sort(key=lambda item: (item[0], item[1], item[2].column))
        chosen = reachable[0][2]
        return chosen, f"{chosen.qualified}, below the subject"
    if above:
        chosen = sorted(above, key=lambda m: m.column)[0]
        return chosen, (f"{chosen.qualified}, above the subject: the pattern's "
                        f"share is drawn over {chosen.table.split('.')[-1]}")
    if refused:
        return None, (
            f"{element} is only on {', '.join(refused)}, which is generated before "
            f"the subject is. Picking a share of the subject cannot reach back and "
            f"change what its parent already holds, so this has to be stated as a "
            f"pattern about that entity instead"
        )
    return None, (
        f"{element} is mapped only where a pattern cannot reach it: {', '.join(elsewhere)}"
    )


def _enrolled_above(
    element: str, parameter: PatternParameter, *, subject: str,
    mappings: MappingStore, plan: Plan,
) -> ColumnMapping | None:
    """Where a member's enrollment is held, when that is above the member.

    A member's plan and group are usually written on the subscriber, which is
    generated before the member. Any other attribute up there is the parent's
    own and out of a member pattern's reach; enrollment is not: every member
    of a family is on the subscriber's plan. So the pattern states it there,
    and its share is drawn over subscribers, each family wholly in or out.
    """
    if parameter.record_group != "enrollment":
        return None
    above = ancestry(plan, subject)
    carriers = [m for m in mappings.mappings
                if m.element == element and m.table in above
                and _mode(plan, m.table) == "generate" and m.bucket != "business_key"]
    if not carriers:
        return None
    return sorted(carriers, key=lambda m: (above.index(m.table), m.column))[0]


def _depth(plan: Plan, table: str, subject: str) -> int:
    chain = ancestry(plan, table)
    return chain.index(subject) + 1 if subject in chain else 0


def _refuse_field(
    mapping: ColumnMapping, field: PlanField, parameter: "PatternParameter | None" = None
) -> str | None:
    """Whether this column is one a pattern is allowed to state."""
    # A reference can be narrowed to the rows a pattern names, which is a
    # different thing from replacing it: the column still points at real rows,
    # just fewer of them.
    if field.generator == "reference" and parameter is not None and parameter.values:
        return None
    if field.generator in REFUSALS:
        return REFUSALS[field.generator]
    if field.generator not in REPLACEABLE:
        return f"the column is generated by {field.generator}, which a pattern cannot replace"
    if field.params.get("unique") or mapping.bucket == "business_key":
        return "the column is a key and every row needs its own value"
    return None


# ---------------------------------------------------------------------------
# Values to rules
# ---------------------------------------------------------------------------


def _build_literal(parameter: PatternParameter, *, field: PlanField) -> dict[str, Any]:
    """A stated value becomes a rule the engine already runs.

    The value is checked against what the column actually holds first. A code
    the column has never held, or a string its format rejects, is a
    mistranslation between the analyzer's vocabulary and the database's, and it
    is far better to say so here than to write it and fail a check afterwards.
    """
    if parameter.record_field and parameter.values:
        # Whole records, drawn under one key by every column that takes a
        # field of them.
        params = {"values": list(parameter.values), "record_key": parameter.record_key(),
                  "field": parameter.record_field}
        within = (field.params.get("parent_table") if field.generator == "reference"
                  else field.params.get("within"))
        if within:
            # The column points into a table; it still draws only values that
            # table holds, so a combination never writes a code the target lacks.
            params["within"] = within
        if parameter.weights:
            params["weights"] = list(parameter.weights)
        return {"generator": "lookup", "params": params}

    if parameter.values:
        values = [str(v) for v in parameter.values]
        if field.generator == "reference":
            # Narrowed, not replaced: the column keeps pointing at the table it
            # pointed at, drawing from the rows the pattern named.
            params: dict[str, Any] = dict(field.params)
            params["keys"] = values
            if parameter.weights:
                if len(parameter.weights) != len(values):
                    raise PatternError(
                        f"{len(values)} values but {len(parameter.weights)} weights"
                    )
                params["weights"] = list(parameter.weights)
            return {"generator": "reference", "params": params}

        _check_against_domain(values, field)
        params = {"values": values}
        if parameter.weights:
            if len(parameter.weights) != len(values):
                raise PatternError(
                    f"{len(values)} values but {len(parameter.weights)} weights"
                )
            params["weights"] = list(parameter.weights)
        return {"generator": "lookup", "params": params}

    # A band on a numeric column is a range to draw from, not a point. The
    # analyzer measures a cluster's spread, and collapsing it would generate a
    # narrower population than the one it found.
    if parameter.min is not None and parameter.max is not None:
        if field.generator == "number":
            params = dict(field.params)
            params.update(
                distribution="uniform",
                min=float(parameter.min),
                max=float(parameter.max),
            )
            params.pop("mean", None)
            params.pop("stddev", None)
            if parameter.bands:
                params["bands"] = [list(b) for b in parameter.bands]
            return {"generator": "number", "params": params}
        raise PatternError(
            f"this is a range, and {field.generator} does not take one; the column "
            f"would have to hold numbers for a range to mean anything"
        )

    if parameter.value is None:
        raise PatternError("the parameter states no value")

    value = parameter.value
    _check_against_domain([str(value)], field)

    # A date is expressed as the existing relative-date rule with no gap, so it
    # reaches the database as a date rather than as a string that looks like one.
    if field.generator == "date_relative" and _is_iso_date(str(value)):
        return {
            "generator": "date_relative",
            "params": {"anchor": str(value)[:10], "min_days": 0, "max_days": 0},
        }
    return {"generator": "constant", "params": {"value": value}}


def _check_against_domain(values: list[str], field: PlanField) -> None:
    """Refuse a value the column was never measured holding."""
    if field.generator == "lookup" and not field.params.get("record_key"):
        known = [str(v) for v in field.params.get("values") or [] if not isinstance(v, dict)]
        unknown = [v for v in values if v not in known]
        if known and unknown:
            shown = ", ".join(sorted(known)[:12])
            raise PatternError(
                f"{', '.join(unknown)} is not one of the values this column holds ({shown}); "
                f"the analyzer's code may need translating"
            )
    if field.generator == "pattern" and field.params.get("regex"):
        regex = str(field.params["regex"])
        bad = [v for v in values if matches_format([v], regex) < 1.0]
        if bad:
            raise PatternError(
                f"{', '.join(bad)} does not match the format {regex} this column holds"
            )
    if field.generator == "number":
        low, high = field.params.get("min"), field.params.get("max")
        for raw in values:
            try:
                number = float(raw)
            except ValueError as exc:
                raise PatternError(
                    f"{raw!r} is not a number, and this column holds numbers"
                ) from exc
            if low is not None and number < float(low):
                raise PatternError(f"{raw} is below the {low} this column was measured at")
            if high is not None and number > float(high):
                raise PatternError(f"{raw} is above the {high} this column was measured at")


def _build_derived(
    spec: dict[str, Any],
    parameter: PatternParameter,
    *,
    table_name: str,
    mappings: MappingStore,
    as_of: str | None,
) -> dict[str, Any]:
    """A parameter that is not a column: express it on the column that is."""
    low, high = _band(parameter)

    if spec["element"] in (DERIVATIONS["age"]["element"], DERIVATIONS["tenure"]["element"]):
        if not as_of:
            raise PatternError(
                f"{spec['label'].lower()} needs a reference date; set as_of on the "
                f"pattern or pass --as-of, because nothing here may read the clock"
            )
        reference = _parse_date(as_of)
        if spec["element"] == "DOB":
            # Someone aged `low` on the reference date was born no later than
            # that many years before it, and someone aged `high` no earlier
            # than a year beyond that.
            latest = _minus_years(reference, low)
            earliest = _minus_years(reference, high + 1)
        else:
            # Tenure arrives in months and counts forward from the start of
            # coverage, so the window is the mirror image of the age one.
            latest = reference - timedelta(days=int(low * 30.44))
            earliest = reference - timedelta(days=int((high + 1) * 30.44))
        params = {
            "anchor": reference.isoformat(),
            "min_days": (earliest - reference).days + 1,
            "max_days": (latest - reference).days,
        }
        if parameter.bands:
            # The pattern's spread: each slice of ages (months) a window of days
            # back from the reference date, drawn at its share.
            per = 365.25 if spec["element"] == "DOB" else 30.44
            params["bands"] = [[-int(round(b_high * per)), -int(round(b_low * per)), share]
                               for b_low, b_high, share in parameter.bands]
        return {"generator": "date_relative", "params": params}

    anchor_element = spec.get("anchor_element")
    anchor_column = next(
        (
            m.column for m in mappings.mappings
            if m.element == anchor_element and m.table == table_name
        ),
        None,
    )
    if anchor_column is None:
        raise PatternError(
            f"{anchor_element} is not mapped on {table_name}, so there is nothing to "
            f"measure the gap from"
        )
    return {
        "generator": "date_relative",
        "params": {"anchor": anchor_column, "min_days": low, "max_days": high},
    }


def _band(parameter: PatternParameter) -> tuple[int, int]:
    """The low and high of a stated quantity. A point value is a band of one."""
    def whole(value: Any) -> int:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise PatternError(
                f"{value!r} is not a number of {parameter.unit or 'units'}"
            ) from exc

    if parameter.min is not None and parameter.max is not None:
        low, high = whole(parameter.min), whole(parameter.max)
        if low > high:
            raise PatternError(f"the lowest value {low} is above the highest {high}")
        return low, high
    if parameter.value is None:
        raise PatternError("the parameter states no value")
    point = whole(parameter.value)
    return point, point


def _minus_years(reference: date, years: int) -> date:
    try:
        return reference.replace(year=reference.year - years)
    except ValueError:  # 29 February against a non-leap year
        return reference.replace(year=reference.year - years, day=28)


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError as exc:
        raise PatternError(f"{text!r} is not an ISO date") from exc


def _is_iso_date(text: str) -> bool:
    try:
        date.fromisoformat(text[:10])
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Columns that move together
# ---------------------------------------------------------------------------


def _shared_group(table: PlanTable | None, field: PlanField) -> list[PlanField]:
    """Fields that draw the same record as this one, this one included.

    ZIP, city, and state name the same record key so they stay consistent. They
    draw the same index out of their own value lists, so a list filtered for one
    of them has to be filtered for all of them or the three come apart.
    """
    key = field.params.get("record_key")
    if table is None or not key or field.generator != "lookup":
        return []
    return [
        f for f in table.fields
        if f.generator == "lookup" and f.params.get("record_key") == key
    ]


def _filter_shared_record(
    group: list[PlanField], *, field: PlanField, value: Any
) -> list[tuple[PlanField, str, dict[str, Any], str | None]] | None:
    """Narrow every column in the group to the records that match the value."""
    inner = field.params.get("field")
    records = [r for r in field.params.get("values") or [] if isinstance(r, dict)]
    kept = [r for r in records if str(r.get(inner)) == str(value)]
    if not kept:
        return None
    out: list[tuple[PlanField, str, dict[str, Any], str | None]] = []
    for sibling in group:
        params = dict(sibling.params)
        params["values"] = kept
        others = ", ".join(sorted(f.name for f in group if f is not sibling))
        out.append(
            (
                sibling,
                "lookup",
                params,
                f"narrowed to {len(kept)} records and drawn with {others}, so the "
                f"{field.params.get('record_key')} stays consistent",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Reading and applying
# ---------------------------------------------------------------------------


def load_document(path: str | Path) -> PatternDocument:
    """Read the analyzer's output, in any of the shapes it sensibly comes in."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PatternError(f"{path} is not valid JSON: {exc}") from exc
    # One pattern on its own is accepted, because that is what selecting a
    # single pattern in the analyzer produces.
    if isinstance(raw, list):
        raw = {"patterns": raw}
    elif isinstance(raw, dict) and "patterns" not in raw and "parameters" in raw:
        raw = {"patterns": [raw]}
    try:
        document = PatternDocument.model_validate(raw)
    except Exception as exc:
        raise PatternError(f"{path} is not an analyzer document: {exc}") from exc
    if not document.patterns:
        raise PatternError(f"{path} holds no patterns")
    return document


def resolve_document(
    document: PatternDocument,
    *,
    catalog: Catalog,
    mappings: MappingStore,
    plan: Plan,
    model: PhysicalModel | None = None,
    as_of: str | None = None,
) -> list[Resolution]:
    return [
        resolve_pattern(
            pattern,
            catalog=catalog,
            mappings=mappings,
            plan=plan,
            model=model,
            as_of=as_of or document.as_of,
        )
        for pattern in document.patterns
    ]


def apply_to_decisions(
    resolutions: list[Resolution],
    decisions: dict[str, Any],
    *,
    as_cohort: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Write resolved targets into the decisions store as approved overrides.

    This is the whole of applying a pattern: a target is a column, a generator,
    and its parameters, which is exactly what a decision is. `sdg plan` already
    honors an approved decision and records the rule it displaced, so the plan
    comes out saying the pattern's value is there because someone asked for it.

    Entries carry the pattern they came from, so they can be found and taken out
    again, and a column decided by hand is never overwritten.

    With `as_cohort`, the pattern applies to its own share of the population
    rather than to every row: a cohort is recorded beside the columns, and each
    entry names it. The share comes from the pattern's own support, because a
    prevalence this tool invented would be a number nobody measured.
    """
    document = dict(decisions)
    columns = [dict(entry) for entry in document.get("columns", [])]
    by_key = {(e.get("table"), e.get("column")): i for i, e in enumerate(columns)}
    cohorts = {c["name"]: dict(c) for c in document.get("cohorts", [])}
    written: list[str] = []
    for resolution in resolutions:
        cohort_name: str | None = None
        if as_cohort:
            if not resolution.support:
                raise PatternError(
                    f"{resolution.pattern} states no support, so there is no share to "
                    f"generate. Apply it to the whole run, or give it a support."
                )
            cohort_name = resolution.pattern
            cohorts[cohort_name] = {
                "name": cohort_name,
                # Drawn where its plan is held, so a family is wholly in it.
                "subject": resolution.share_of or resolution.subject,
                "share": round(float(resolution.support), 6),
                "label": resolution.label or None,
            }
        for target in resolution.targets:
            key = (target.table, target.column)
            entry = {
                "table": target.table,
                "column": target.column,
                "approved": True,
                "element": target.element,
                "generator": target.generator,
                "params": target.params,
                "_pattern": resolution.pattern,
                "_why": f"from the analyzer pattern {resolution.pattern}: {target.parameter}",
            }
            if cohort_name:
                # The column keeps the rule the tool worked out; this one applies
                # to the share of rows the pattern covers.
                entry["_cohort"] = cohort_name
                entry["_overrides"] = [_override(entry)]
                entry["_why"] += (
                    f", over {float(resolution.support):.1%} of "
                    f"{(resolution.share_of or resolution.subject).split('.')[-1]}"
                )
                if resolution.share_of and resolution.share_of != resolution.subject:
                    # The share was measured over the subject's rows; drawn
                    # over the table above, each row drawn in brings all of
                    # its own below, so the subject's share lands near it.
                    entry["_why"] += (
                        f" (measured over {resolution.subject.split('.')[-1]}; each "
                        f"{resolution.share_of.split('.')[-1]} drawn in has all of its rows)"
                    )
            if key in by_key:
                existing = columns[by_key[key]]
                if existing.get("approved") and not existing.get("_pattern"):
                    continue  # someone decided this one by hand; leave it alone
                if cohort_name and existing.get("_cohort"):
                    # Several cohorts in one load each state this column: every
                    # one keeps its own rule, not only the last one applied.
                    entry["_overrides"] = [
                        o for o in _overrides(existing) if o["cohort"] != cohort_name
                    ] + entry["_overrides"]
                    if existing.get("_replaced"):
                        entry["_replaced"] = existing["_replaced"]
                    columns[by_key[key]] = entry
                    written.append(f"{target.table}.{target.column}")
                    continue
                # Whatever stood here is carried along, so clearing the pattern
                # puts it back rather than leaving a hole. A column already in
                # the review queue is the common case, and losing its entry
                # would quietly shorten the queue.
                displaced = (
                    existing.get("_replaced") if existing.get("_pattern") else existing
                )
                if displaced:
                    entry["_replaced"] = displaced
                columns[by_key[key]] = entry
            else:
                by_key[key] = len(columns)
                columns.append(entry)
            written.append(f"{target.table}.{target.column}")
    document["columns"] = columns
    if cohorts:
        document["cohorts"] = list(cohorts.values())
    return document, written


def _override(entry: dict[str, Any]) -> dict[str, Any]:
    return {"cohort": entry["_cohort"], "generator": entry["generator"],
            "params": entry.get("params") or {}, "_pattern": entry.get("_pattern")}


def _overrides(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every cohort's rule for one column; an entry written before several
    were kept holds only its own."""
    return list(entry.get("_overrides") or [_override(entry)])


def clear_from_decisions(
    decisions: dict[str, Any], pattern: str | None = None
) -> tuple[dict[str, Any], int]:
    """Take pattern-written entries back out, leaving hand-made ones alone.

    An entry that displaced one is put back as it was, so clearing a pattern
    returns the file to the state applying it found.
    """
    document = dict(decisions)
    kept: list[dict[str, Any]] = []
    removed = 0
    document["cohorts"] = [
        c for c in document.get("cohorts", [])
        if pattern is not None and c.get("name") != pattern
    ] if pattern is not None else []
    if not document["cohorts"]:
        document.pop("cohorts", None)
    for entry in document.get("columns", []):
        came_from = entry.get("_pattern")
        if pattern is not None and entry.get("_cohort") and len(_overrides(entry)) > 1:
            left = [o for o in _overrides(entry) if o.get("_pattern") != pattern]
            if len(left) < len(_overrides(entry)):
                removed += 1
                last = left[-1]
                entry = {**entry, "_overrides": left, "_cohort": last["cohort"],
                         "_pattern": last["_pattern"], "generator": last["generator"],
                         "params": last["params"]}
            kept.append(entry)
            continue
        if came_from and (pattern is None or came_from == pattern):
            removed += 1
            displaced = entry.get("_replaced")
            if displaced:
                kept.append(displaced)
            continue
        kept.append(entry)
    document["columns"] = kept
    return document, removed


# ---------------------------------------------------------------------------
# Did the pattern actually land?
# ---------------------------------------------------------------------------


def measure_cohorts(plan: Plan, result: Any) -> list[dict[str, Any]]:
    """What share of the generated rows each cohort actually got.

    A pattern applied and never checked is a claim nobody tested. The share is
    drawn per row, so it lands near the figure asked for rather than on it, and
    the honest thing is to report both and let the reader judge the gap.

    Costs one pass over the membership flags, which the engine has already
    computed -- no second generation and no query.
    """
    measured: list[dict[str, Any]] = []
    for cohort in plan.cohorts:
        table = result.tables.get(cohort.subject)
        if table is None or not table.tags:
            measured.append({
                "cohort": cohort.name,
                "subject": cohort.subject,
                "asked": cohort.share,
                "actual": None,
                "rows": 0,
                "note": "the subject table was not generated in this run",
            })
            continue
        bit = 1 << next(
            i for i, c in enumerate(plan.cohorts) if c.name == cohort.name
        )
        inside = sum(1 for tag in table.tags if tag & bit)
        total = len(table.tags)
        measured.append({
            "cohort": cohort.name,
            "label": cohort.label,
            "subject": cohort.subject,
            "asked": cohort.share,
            "actual": (inside / total) if total else None,
            "rows": inside,
            "of": total,
        })
    return measured
