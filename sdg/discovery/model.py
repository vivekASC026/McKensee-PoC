"""The physical model: what discovery found in a database.

Stored as JSON so it can be re-discovered later and compared against what was
there before. Every later step reads this rather than going back to the
database for structure.
"""

from __future__ import annotations

import json
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

CHARACTER_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
NUMERIC_TYPES = {
    "tinyint", "smallint", "int", "bigint", "decimal", "numeric",
    "float", "real", "money", "smallmoney",
}
INTEGER_TYPES = {"tinyint", "smallint", "int", "bigint"}
DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


_INDEXES: dict[tuple, tuple] = {}


def indexed(owner: BaseModel, items: list, key, *, many: bool = False) -> dict:
    """A lookup over `items` by `key`, kept for `owner` and rebuilt whenever
    the list is replaced or grows. A database of thousands of tables has tens of
    thousands of columns, and finding each one by reading the list from the
    top is billions of comparisons. The first item with a key wins, as a scan
    from the top would find it; with `many`, every item is kept, in order."""
    name = (id(owner), id(key), many)
    found = _INDEXES.get(name)
    if (found is None or found[0]() is not owner or found[1] is not items
            or found[2] != len(items)):
        index: dict = {}
        for item in items:
            if many:
                index.setdefault(key(item), []).append(item)
            else:
                index.setdefault(key(item), item)
        # Kept beside the object, not in it: two equal models stay equal.
        found = (weakref.ref(owner, lambda _, name=name: _INDEXES.pop(name, None)),
                 items, len(items), index)
        _INDEXES[name] = found
    return found[3]


class Column(Base):
    name: str
    ordinal: int
    type_name: str
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    nullable: bool = False
    is_identity: bool = False
    is_computed: bool = False
    computed_definition: str | None = None
    default_definition: str | None = None
    # A text column's collation. Two text columns in one database can differ,
    # and SQL Server will not compare them as they stand.
    collation: str | None = None
    is_primary_key: bool = False
    primary_key_ordinal: int | None = None
    is_unique: bool = False
    check_definition: str | None = None
    check_values: list[str] | None = None
    # Filled in by the classifier.
    bucket: str | None = None
    bucket_reason: str | None = None

    @property
    def is_character(self) -> bool:
        return self.type_name in CHARACTER_TYPES

    @property
    def is_numeric(self) -> bool:
        return self.type_name in NUMERIC_TYPES

    @property
    def is_integer(self) -> bool:
        return self.type_name in INTEGER_TYPES

    @property
    def is_date(self) -> bool:
        return self.type_name in DATE_TYPES

    def type_description(self) -> str:
        if self.type_name in {"decimal", "numeric"}:
            return f"{self.type_name}({self.precision},{self.scale})"
        if self.is_character and self.max_length is not None:
            length = "max" if self.max_length < 0 else str(self.character_length())
            return f"{self.type_name}({length})"
        return self.type_name

    def character_length(self) -> int | None:
        """Length in characters. sys.columns reports bytes, doubled for n-types."""
        if self.max_length is None or self.max_length < 0:
            return None
        if self.type_name in {"nchar", "nvarchar"}:
            return self.max_length // 2
        return self.max_length


class ForeignKey(Base):
    name: str | None = None
    columns: list[str]
    parent_table: str
    parent_columns: list[str]
    kind: str = "declared"
    coverage: float | None = None
    evidence: list[str] = Field(default_factory=list)


class UniqueConstraint(Base):
    name: str | None = None
    columns: list[str]
    is_primary_key: bool = False


class CheckConstraint(Base):
    name: str
    definition: str
    column: str | None = None
    values: list[str] | None = None


class Table(Base):
    name: str
    schema_name: str
    table_name: str
    row_count: int = 0
    columns: list[Column] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    check_constraints: list[CheckConstraint] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    # Columns that lead an index of any kind: a value of one is found by a
    # seek, so a lookup on it costs the same however large the table is.
    indexed: list[str] = Field(default_factory=list)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def foreign_key_columns(self) -> dict[str, ForeignKey]:
        """Column name to the foreign key it belongs to, declared or inferred."""
        out: dict[str, ForeignKey] = {}
        for fk in self.foreign_keys:
            for column in fk.columns:
                out.setdefault(column, fk)
        return out

    def leads_index(self, name: str) -> bool:
        """Whether a value of this column is found by a seek rather than a scan."""
        return (name in self.indexed or name in self.primary_key[:1]
                or any(c.columns[:1] == [name] for c in self.unique_constraints))

    def unique_single_columns(self) -> set[str]:
        """Columns that are unique on their own."""
        out = set()
        for constraint in self.unique_constraints:
            if len(constraint.columns) == 1:
                out.add(constraint.columns[0])
        return out


class PhysicalModel(Base):
    connection: dict[str, Any] = Field(default_factory=dict)
    discovered: str = ""
    tables: list[Table] = Field(default_factory=list)

    def table(self, name: str) -> Table | None:
        return indexed(self, self.tables, _table_name).get(name)

    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def columns(self) -> Iterator[tuple[Table, Column]]:
        for table in self.tables:
            for column in table.columns:
                yield table, column

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json", exclude_none=True), indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "PhysicalModel":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_name(table: Table) -> str:
    return table.name
