"""The element catalog.

An element is the unit of governance: a business data element the client owns,
carrying the rule for how its values are produced. Physical columns map to
elements and inherit the rule, which is what stops the same Member ID being
redefined in every application.

Inheritance and locks are out of scope for the proof of concept, so an element
here is flat: one level, owned by the organization.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sdg.plan.filters import parse_filter

Rule = Literal["copy", "reuse", "generate"]
PiiClass = Literal["none", "identifier", "sensitive"]
ConsistencyScope = Literal["single_system", "cross_system"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReuseDomain(Base):
    """Where a reuse element draws its values from.

    Bound per environment, deliberately: the element says "plan codes come from
    master data", and the environment says which database that is today.
    """

    connection: str
    table: str
    key_column: str
    schema_: str | None = Field(default=None, alias="schema")
    attributes: list[str] = Field(default_factory=list)
    filter: str | None = None
    load: Literal["full", "sample", "snapshot"] = "full"
    sample_size: int | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("filter")
    @classmethod
    def _filter_is_conditions(cls, value: str | None) -> str | None:
        if value:
            parse_filter(value)  # refused here, when the file is read, not mid-run
        return value


class Element(Base):
    name: str
    description: str = ""
    domain: str = ""
    rule: Rule = "generate"
    generator: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reuse_domain: ReuseDomain | None = None
    copy_from: str | None = None
    format: str | None = None
    check_digit: str | None = None
    pii: PiiClass = "none"
    consistency_scope: ConsistencyScope = "single_system"
    aliases: list[str] = Field(default_factory=list)
    # A vocabulary rather than data: procedure codes, diagnosis codes. Its
    # values come from the pattern store -- the codes the sources actually
    # use -- whichever database is being generated into, so a file never
    # carries a code nobody uses.
    code_set: bool = False

    @model_validator(mode="after")
    def _rule_has_what_it_needs(self) -> "Element":
        if self.rule == "generate" and not self.generator:
            raise ValueError(f"element {self.name}: a generate rule needs a generator")
        if self.rule == "reuse" and self.reuse_domain is None:
            raise ValueError(f"element {self.name}: a reuse rule needs a reuse_domain")
        if self.rule == "copy" and not self.copy_from:
            raise ValueError(f"element {self.name}: a copy rule needs copy_from")
        return self

    def all_names(self) -> list[str]:
        """The element name plus every alias, deduplicated."""
        seen = dict.fromkeys([self.name, *self.aliases])
        return list(seen)


class Catalog(Base):
    version: str = "0.1.0"
    organization: str = ""
    description: str = ""
    elements: list[Element] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_are_unique(self) -> "Catalog":
        names = [e.name for e in self.elements]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate elements in catalog: {sorted(duplicates)}")
        return self

    def element(self, name: str) -> Element | None:
        return next((e for e in self.elements if e.name == name), None)

    def by_rule(self, rule: Rule) -> list[Element]:
        return [e for e in self.elements if e.rule == rule]

    def summary(self) -> dict[str, Any]:
        return {
            "elements": len(self.elements),
            "rules": {
                rule: len(self.by_rule(rule)) for rule in ("generate", "reuse", "copy")
            },
            "with_format": sum(1 for e in self.elements if e.format),
            "pii": {
                kind: sum(1 for e in self.elements if e.pii == kind)
                for kind in ("none", "identifier", "sensitive")
            },
            "aliases": sum(len(e.aliases) for e in self.elements),
        }
