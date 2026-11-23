"""Resolve every column to one rule, and record where that rule came from.

Precedence, from design document section 6.6:

    structural   identity, computed, foreign key, composite key. Never
                 overridden: the database's own shape decides these.
    user         a decision recorded in review-decisions.json.
    catalog      the rule on the element the column maps to, when the mapping
                 was accepted or the user approved it.
    learned      the proposal the profiler made from the data.
    physical     the column's type and length, as a last resort.

One exception is worth stating plainly. The catalog governs what a value *is*:
its format, its allowed set, where it is drawn from. It cannot express what a
value *depends on*, because a dependency is a property of this schema rather
than of the element. So when the profiler found that a date follows another
column, or that an amount is the sum of its children, that rule wins over the
catalog's generic default and the plan records the override. A catalog entry
saying "a service date is some date after 2020" is a placeholder; "a service
date falls inside the member's coverage" is the real rule.

Whatever loses is kept on the field, so the provenance panel can show what the
winning rule beat and why.
"""

from __future__ import annotations

from typing import Any

from sdg.catalog.model import Catalog, Element
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.mapping.scorer import MappingStore
from sdg.plan.model import Evidence, OverriddenRule, PlanField
from sdg.plan.proposer import Proposal, clamp_coverage

# Rules the catalog cannot express, so a learned one of these wins.
DEPENDENT_GENERATORS = {"expression"}


def _evidence_from(items: list[dict[str, Any]]) -> list[Evidence]:
    return [
        Evidence(
            kind=item["kind"],
            detail=item.get("detail", ""),
            coverage=item.get("coverage"),
            score=item.get("score"),
        )
        for item in items
    ]


def _element_field(
    element: Element, table: Table, element_by_column: dict[str, str]
) -> tuple[str, dict[str, Any]] | None:
    """The generator and params an element contributes, or None if it cannot.

    Anchors written as element names are translated to the column in this table
    that carries that element, which is what lets one catalog entry say
    "termination follows effective" across systems that name those columns
    differently.
    """
    if element.rule == "copy":
        return None
    if element.rule == "reuse":
        return None
    if not element.generator:
        return None

    params = dict(element.params)
    if element.generator == "date_relative":
        anchor = params.get("anchor")
        if isinstance(anchor, str) and not _looks_like_a_date(anchor):
            column_name = element_by_column.get(anchor)
            if column_name is None or table.column(column_name) is None:
                return None
            params["anchor"] = column_name
    return element.generator, params


def _looks_like_a_date(text: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(text[:10])
        return True
    except ValueError:
        return False


def _learned_beats_catalog(proposal: Proposal, element_generator: str) -> bool:
    """Does the proposal express something the catalog cannot?"""
    if proposal.generator in DEPENDENT_GENERATORS:
        return True
    if proposal.generator == "date_relative" and element_generator == "date_relative":
        # A column anchor is a real dependency; a literal date is a placeholder.
        anchor = proposal.params.get("anchor", "")
        return isinstance(anchor, str) and not _looks_like_a_date(anchor)
    return False


def resolve_column(
    table: Table,
    column: Column,
    proposal: Proposal,
    mapping_element: str | None,
    catalog: Catalog,
    element_by_column: dict[str, str],
    decision: dict[str, Any] | None = None,
) -> PlanField:
    """Apply precedence to one column and return the resolved field."""
    overrode: list[OverriddenRule] = []
    element = catalog.element(mapping_element) if mapping_element else None

    generator = proposal.generator
    params = dict(proposal.params)
    provenance = proposal.provenance
    confidence = proposal.confidence
    coverage = clamp_coverage(proposal.coverage)
    evidence = _evidence_from(proposal.evidence)
    note = proposal.note
    user_override = False

    structural = proposal.provenance == "structural"

    # --- catalog ---------------------------------------------------------
    if not structural and element is not None:
        contribution = _element_field(element, table, element_by_column)
        if contribution is not None:
            element_generator, element_params = contribution
            from sdg.plan.editing import check_fits

            misfit = check_fits(element_generator, dict(element_params), column)
            if misfit:
                # The column's own type decides: a dictionary of names cannot
                # be offered to a column four characters wide.
                overrode.append(
                    OverriddenRule(
                        provenance="catalog",
                        generator=element_generator,
                        reason=f"{element.name}'s rule does not fit: {misfit[0]}",
                    )
                )
            elif _learned_beats_catalog(proposal, element_generator):
                overrode.append(
                    OverriddenRule(
                        provenance="catalog",
                        generator=element_generator,
                        reason=(
                            f"{element.name} supplies a default, but the data shows a "
                            f"dependency the catalog cannot express"
                        ),
                    )
                )
            else:
                if generator != element_generator or params != element_params:
                    overrode.append(
                        OverriddenRule(
                            provenance=proposal.provenance,
                            generator=proposal.generator,
                            reason=f"the catalog governs this value through {element.name}",
                        )
                    )
                generator, params = element_generator, dict(element_params)
                provenance = "catalog"
                confidence = "high"
                coverage = 1.0
                evidence = [
                    Evidence(
                        kind="catalog_element",
                        detail=(
                            f"{element.name}: {element.description or 'no description'}"
                        ),
                    ),
                    *evidence,
                ]
        elif element.rule == "copy":
            note = (
                f"{element.name} carries the copy rule, which the proof of concept "
                f"does not implement; the learned rule is used instead"
            )

    # --- user ------------------------------------------------------------
    if decision and decision.get("approved"):
        user_override = True
        chosen_element = decision.get("element")
        if decision.get("generator"):
            if decision["generator"] != generator or decision.get("params") != params:
                overrode.append(
                    OverriddenRule(
                        provenance=provenance,
                        generator=generator,
                        reason="a person changed this in review-decisions.json",
                    )
                )
            generator = decision["generator"]
            params = dict(decision.get("params") or {})
        if not structural:
            provenance = "user"
            confidence = "high"
        if chosen_element:
            mapping_element = chosen_element
        evidence = [
            Evidence(kind="user_decision", detail="approved during review"),
            *evidence,
        ]

    # A note is the proposer or the resolver saying something a user needs to
    # read. It rides along as evidence rather than being dropped on the floor.
    if note:
        evidence = [*evidence, Evidence(kind="note", detail=note)]

    return PlanField(
        name=column.name,
        bucket=column.bucket or "free",
        element=mapping_element,
        generator=generator,
        params=params,
        confidence=confidence,
        evidence=evidence or None,
        coverage=coverage,
        provenance=provenance,
        user_override=user_override,
        overrode=overrode or None,
        data_type=column.type_description(),
        nullable=column.nullable,
        null_rate=_null_rate(proposal, column),
    )


def _null_rate(proposal: Proposal, column: Column) -> float | None:
    """Carry the observed null rate onto nullable columns so it is reproduced."""
    if not column.nullable:
        return None
    for item in proposal.evidence:
        if item["kind"] == "null_rate" and item.get("coverage") is not None:
            return round(float(item["coverage"]), 6)
    return None


def element_index(
    model: PhysicalModel, mappings: MappingStore
) -> dict[str, dict[str, str]]:
    """Per table, which column carries each element. Used to resolve anchors."""
    index: dict[str, dict[str, str]] = {}
    for mapping in mappings.mappings:
        if mapping.element:
            index.setdefault(mapping.table, {}).setdefault(mapping.element, mapping.column)
    return index


def decisions_index(decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{entry['table']}.{entry['column']}": entry
        for entry in decisions.get("columns", [])
        if entry.get("table") and entry.get("column")
    }
