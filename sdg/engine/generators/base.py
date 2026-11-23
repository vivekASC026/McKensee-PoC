"""Generator contract and registry.

Every generator is a pure function of (params, seed, row index, row context).
No generator holds state between calls, which is what makes a run reproducible
and lets fields be evaluated in any order the dependency graph allows.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Iterable

from sdg.engine.seed import ValueRandom, value_seed


class DeferredValue:
    """Placeholder for a value that cannot be computed until a later table runs.

    A claim total that sums its claim lines is the case this exists for: the
    claim row is built before its lines, so the field is left deferred and
    filled in once the child table has been generated.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<deferred: {self.reason}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DeferredValue) and other.reason == self.reason

    def __hash__(self) -> int:
        return hash(("deferred", self.reason))


class GenerationError(RuntimeError):
    """A generator could not produce a value."""


@dataclass
class RowContext:
    """Everything a generator may read while producing one value."""

    seed: str
    scenario: str
    table: str
    field_name: str
    params: dict[str, Any]
    row_index: int
    row: dict[str, Any] = dataclass_field(default_factory=dict)

    # The row of each table this row points at, keyed by qualified table name.
    parents: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    # Index of each parent row inside its own table.
    parent_indices: dict[str, int] = dataclass_field(default_factory=dict)
    # Key values already generated for each table, for hash picks.
    parent_keys: dict[str, list[Any]] = dataclass_field(default_factory=dict)
    # Attribute rows for reference sets, keyed by table then key value.
    parent_attributes: dict[str, dict[Any, dict[str, Any]]] = dataclass_field(default_factory=dict)
    # Position of this row inside its driving parent, for per-parent sequences.
    child_index: int = 0
    driving_parent: str | None = None

    def stream(self, salt: str = "") -> ValueRandom:
        """A value stream for this field and row.

        `salt` separates independent draws inside one generator, such as the
        null decision from the value itself.
        """
        key = self.field_name if not salt else f"{self.field_name}#{salt}"
        return ValueRandom(value_seed(self.seed, self.scenario, key, self.row_index))

    def field_stream(self, salt: str = "") -> ValueRandom:
        """A stream for the field as a whole, the same for every row.

        Used for per-column constants such as where a unique pattern walk
        starts; mixing the row index in here would move the walk every row.
        """
        key = self.field_name if not salt else f"{self.field_name}#{salt}"
        return ValueRandom(value_seed(self.seed, self.scenario, key, -1))

    def shared_stream(self, key: str, salt: str = "") -> ValueRandom:
        """A stream keyed by something other than the field name.

        Columns that must move together, such as ZIP, city, and state, name the
        same key and therefore draw the same record.
        """
        full = key if not salt else f"{key}#{salt}"
        return ValueRandom(value_seed(self.seed, self.scenario, full, self.row_index))

    def require(self, name: str) -> Any:
        if name not in self.params:
            raise GenerationError(
                f"{self.table}.{self.field_name}: generator needs the {name!r} parameter"
            )
        return self.params[name]

    def resolve_name(self, reference: str) -> Any:
        """Resolve `FIELD` in this row or `schema.table.FIELD` in a parent row."""
        if reference in self.row:
            return self.row[reference]
        if "." in reference:
            table, _, column = reference.rpartition(".")
            parent = self.parents.get(table)
            if parent is None:
                raise GenerationError(
                    f"{self.table}.{self.field_name}: no parent row for {table!r}; "
                    f"available: {sorted(self.parents)}"
                )
            if column not in parent:
                raise GenerationError(
                    f"{self.table}.{self.field_name}: {table} has no field {column!r}"
                )
            return parent[column]
        raise GenerationError(
            f"{self.table}.{self.field_name}: cannot resolve {reference!r}; "
            f"fields available: {sorted(self.row)}"
        )

    def can_resolve(self, reference: str) -> bool:
        if reference in self.row:
            return True
        if "." in reference:
            table, _, column = reference.rpartition(".")
            parent = self.parents.get(table)
            return parent is not None and column in parent
        return False


GeneratorFn = Callable[[RowContext], Any]

# Every algorithm by name and version. An algorithm whose output changes is
# registered again under the next version and the old one stays, so a file
# written by an earlier SDM can be written again exactly: its request names
# the versions it was made with (`pinned`). tests/test_algorithm_versions.py
# holds each version's output, so changing a version's output fails there.
_VERSIONS: dict[str, dict[int, Callable[..., Any]]] = {}

# The versions the value being made must use, where a request names them.
_PINNED: ContextVar[dict[str, int] | None] = ContextVar("pinned_algorithms", default=None)


def register_algorithm(name: str, version: int, fn: Callable[..., Any]) -> None:
    held = _VERSIONS.setdefault(name, {})
    if version in held:
        raise RuntimeError(f"algorithm {name!r} version {version} is already registered")
    held[version] = fn


def algorithm(name: str) -> Callable[..., Any]:
    """The algorithm's version this value is made with: the pinned one, else the latest."""
    held = _VERSIONS.get(name)
    if not held:
        raise GenerationError(f"unknown generator {name!r}; known: {sorted(_VERSIONS)}")
    pins = _PINNED.get()
    # A request's versions name every algorithm there was when it was written;
    # one they do not name came later, and its version 1 is what was done
    # before it -- so a new algorithm's version 1 never changes a value.
    wanted = max(held) if pins is None else pins.get(name, 1)
    if wanted not in held:
        raise GenerationError(
            f"{name} version {wanted} is not in this SDM (it has {sorted(held)}): "
            f"the file was written by a newer SDM")
    return held[wanted]


def algorithm_versions() -> dict[str, int]:
    """Each algorithm's latest version: what a file written now is made with."""
    return {name: max(held) for name, held in sorted(_VERSIONS.items())}


@contextmanager
def pinned(versions: dict[str, int] | None):
    """Values made inside use these versions; an algorithm not named, version 1."""
    token = _PINNED.set(versions)
    try:
        yield
    finally:
        _PINNED.reset(token)


def register(name: str, version: int = 1) -> Callable[[GeneratorFn], GeneratorFn]:
    def decorate(fn: GeneratorFn) -> GeneratorFn:
        register_algorithm(name, version, fn)
        return fn

    return decorate


def get_generator(name: str) -> GeneratorFn:
    return algorithm(name)


def registered_names() -> Iterable[str]:
    return sorted(_VERSIONS)
