"""What the landscape looks like, said in a way a target can use.

A profile is keyed by table and column, which makes it a description of one
database and useless about any other. The landscape is the same measurements
keyed by **element** instead: not "sdm.member_denorm.MEME_BIRTH_DT holds dates
between these two" but "DOB holds dates between these two". A target that has
a date of birth somewhere can then be told what one looks like, whatever it
calls the column.

That is the same join a pattern already uses to cross from one landscape to
another, applied one level down -- a pattern says which values, the landscape
says what the values are like when nothing says otherwise.

Relationships work the same way one level up. "Orders per customer" is a shape
of a relationship between two subjects, not of two particular tables, so it is
keyed by the elements the keys carry.

Derived from the central store, because that is where every source has already
been scooped to. A target is never read for this: it supplies a schema and
nothing else, and may hold no rows at all.
"""

from sdg.landscape.model import (
    ElementShape,
    LandscapeShape,
    RelationshipShape,
)
from sdg.landscape.derive import derive, load, save

__all__ = [
    "ElementShape",
    "LandscapeShape",
    "RelationshipShape",
    "derive",
    "load",
    "save",
]
