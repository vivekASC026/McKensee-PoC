"""The shape of a landscape, keyed by element rather than by column."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ElementShape(Base):
    """What one business element looks like across the landscape.

    Everything here was measured, and `observed_in` says where, because a shape
    with no provenance is indistinguishable from a guess. Where several columns
    carry the same element the widest reading wins: a date of birth seen on the
    member and on the subscriber is one element, and a target asking what one
    looks like should be told about both.
    """

    element: str
    observed_in: list[str] = Field(default_factory=list)
    rows: int = 0

    null_rate: float = 0.0
    cardinality: str = "unknown"
    distinct_ratio: float = 0.0

    minimum: Any = None
    maximum: Any = None
    mean: float | None = None
    stddev: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    has_time_component: bool | None = None

    # The values themselves, where there are few enough for that to mean
    # something. A code column is its value set; a name column is not.
    values: list[dict[str, Any]] | None = None
    format_regex: str | None = None
    distribution: dict[str, Any] | None = None

    @property
    def is_coded(self) -> bool:
        return bool(self.values)


class RelationshipShape(Base):
    """How many children a parent has, between two kinds of thing.

    Keyed by the elements the two keys carry rather than by the tables, so a
    target whose customer key is a different column in a differently named
    table can still be told that this landscape holds four to five of these per
    one of those.
    """

    child_element: str
    parent_element: str
    # The store tables it was measured between. Elements identify the key; the
    # tables identify the two kinds of thing, which is what a person maps their
    # own tables onto when the key elements do not line up.
    child_table: str = ""
    parent_table: str = ""
    observed_in: list[str] = Field(default_factory=list)

    parents: int = 0
    children: int = 0
    minimum: int = 0
    maximum: int = 0
    mean: float = 0.0
    stddev: float = 0.0
    median: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    histogram: list[dict[str, Any]] | None = None


class SumShape(Base):
    """A total that is its parts added up, between elements.

    "A claim's charge is its lines' charges summed" -- read from the store,
    where the total and the parts sit on the same flat row, and said by the
    elements of the total, the part and the key that groups them, so a target
    with no rows can still be told its total is a sum and not a number of its
    own.
    """

    total_element: str
    part_element: str
    key_element: str
    observed_in: list[str] = Field(default_factory=list)
    groups: int = 0
    matched: int = 0


class LandscapeShape(Base):
    """Everything the store can say about what data looks like.

    `source` names which extract it was derived from, because a landscape is
    the shape of a book and two books are two shapes. Deriving across all of
    them at once would describe a population nobody has.
    """

    source: str
    derived_from: str = ""
    rows_seen: int = 0
    elements: list[ElementShape] = Field(default_factory=list)
    relationships: list[RelationshipShape] = Field(default_factory=list)
    sums: list[SumShape] = Field(default_factory=list)
    # Elements that differ between the lines of one claim -- a line's charge,
    # its procedure -- as against those a claim carries once. What a claim
    # file needs to know to build claims without any target's plan.
    line_elements: list[str] = Field(default_factory=list)
    # Columns the store holds that matched no element. Said out loud rather
    # than dropped: an unmatched column is a gap in the catalog, and a target
    # asking about that element will be told nothing is known.
    unmatched: list[str] = Field(default_factory=list)
    # Columns that hold no value at all. They describe nothing, so they are
    # kept out of every element rather than matched by their name alone --
    # which is how an empty Medicare-number column came to sit under marital
    # status.
    empty: list[str] = Field(default_factory=list)

    def element(self, name: str) -> ElementShape | None:
        return next((e for e in self.elements if e.element == name), None)

    def relationship(self, child: str, parent: str) -> RelationshipShape | None:
        return next(
            (
                r for r in self.relationships
                if r.child_element == child and r.parent_element == parent
            ),
            None,
        )

    def between(self, child_table: str, parent_table: str) -> RelationshipShape | None:
        """The relationship measured between two store tables.

        How a stated subject mapping finds its ratio: a person says their
        orders are like these claim lines and their customers like these
        members, and this is what that pair was measured to be.
        """
        return next(
            (
                r for r in self.relationships
                if r.child_table == child_table and r.parent_table == parent_table
            ),
            None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "elements": len(self.elements),
            "coded": sum(1 for e in self.elements if e.is_coded),
            "relationships": len(self.relationships),
            "unmatched": len(self.unmatched),
            "rowsSeen": self.rows_seen,
        }
