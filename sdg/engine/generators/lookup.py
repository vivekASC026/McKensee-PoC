"""Lookup generator.

Draws from a list, optionally weighted. Values may be plain scalars or records;
with records, several columns can name the same `record_key` and will draw the
same record, which is how a functional dependency such as ZIP to city and state
is generated as a unit rather than independently.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Iterator, Sequence

from sdg.engine.generators.base import GenerationError, RowContext, register
from sdg.engine.seed import ValueRandom, running_totals

# The caches below are keyed by the lists' ids and hold the lists themselves,
# so an id is never reused by another list while its entry is kept; a hit is
# taken only when the very same lists come back. They are kept only while a
# plan runs (`caching`), emptied as it starts and ends; a rule's value, made
# one at a time from lists built for it, is worked out afresh as before.
_MOST = 20_000
_CACHING: ContextVar[bool] = ContextVar("lookup_caching", default=False)


@contextmanager
def caching() -> Iterator[None]:
    """Keep the caches for one run, and let them go when it ends."""
    forget()
    token = _CACHING.set(True)
    try:
        yield
    finally:
        _CACHING.reset(token)
        forget()


def caching_each(chunks: Iterable[Any]) -> Iterator[Any]:
    """`chunks`, each made with the caches kept (a file written as a stream).

    A stream is drawn one piece at a time, each perhaps from another thread
    with a copy of the context, so the switch is set and reset around every
    piece, never across one; the caches themselves are kept from piece to
    piece and let go when the stream ends."""
    pieces = iter(chunks)
    try:
        while True:
            token = _CACHING.set(True)
            try:
                piece = next(pieces)
            except StopIteration:
                return
            finally:
                _CACHING.reset(token)
            yield piece
    finally:
        close = getattr(pieces, "close", None)
        if close is not None:
            close()
        forget()


def _cached(cache: dict, key: tuple, held: tuple, make):
    if not _CACHING.get():
        return make()
    entry = cache.get(key)
    if entry is not None and all(a is b for a, b in zip(entry[0], held)):
        return entry[1]
    if len(cache) >= _MOST:
        cache.clear()
    made = make()
    cache[key] = (held, made)
    return made


# Narrowed lists, made once per list and table rather than once per row.
_WITHIN: dict[tuple, tuple] = {}


def _within(values, weights, record_field, table, keys):
    def make():
        held = {str(k).strip() for k in keys}
        # An empty field -- no network, out of network -- points at nothing,
        # so it is never outside the table's keys.
        keep = [i for i, r in enumerate(values)
                if isinstance(r, dict) and (r.get(record_field) is None
                                            or str(r.get(record_field, "")).strip() in held)]
        narrowed_weights = ([weights[i] for i in keep]
                            if weights and len(weights) == len(values) else None)
        return [values[i] for i in keep], narrowed_weights

    return _cached(_WITHIN, (id(values), id(weights), record_field, table, len(keys)),
                   (values, weights), make)


_SPREAD: dict[tuple, tuple] = {}


def _spread(values, fields) -> dict[str, int]:
    """How many different values each field takes across the records."""
    return _cached(_SPREAD, (id(values), tuple(sorted(fields))), (values,),
                   lambda: {f: len({_text(r.get(f)) for r in values if isinstance(r, dict)})
                            for f in fields})


_RUNNING: dict[tuple, tuple] = {}


def _weighted(rnd: ValueRandom, weights) -> int:
    """`rnd.weighted_index(weights)`, with the list summed once, not per draw."""
    return rnd.cumulative_index(
        _cached(_RUNNING, (id(weights),), (weights,), lambda: running_totals(weights)))


_AGREEING: dict[tuple, tuple] = {}


def forget() -> None:
    """Empty the caches: one run's lists never serve another."""
    for cache in (_WITHIN, _SPREAD, _RUNNING, _AGREEING):
        cache.clear()


def _agreeing(values, weights, want, nearest):
    """The records, and their weights, that agree with the fields held.

    The same for every row holding the same values, so it is worked out once
    per list and values held rather than by a pass over the records each row.
    """
    def make():
        keep = [i for i, r in enumerate(values) if isinstance(r, dict)
                and all(_text(r.get(f)) == v for f, v in want.items())]
        if not keep and len(want) > 1:
            # Nothing agrees with all of them: let go of the one that says
            # least about the record -- the group, of a handful, before
            # the plan, which also settles the product.
            spread = _spread(values, want)
            for dropped in sorted(want, key=lambda f: (spread[f], f)):
                keep = [i for i, r in enumerate(values) if isinstance(r, dict)
                        and all(_text(r.get(f)) == v for f, v in want.items()
                                if f != dropped)]
                if keep:
                    break
        if not keep and nearest and len(nearest) < len(want):
            # Nothing agrees with every row above: the row this one hangs
            # from comes first. A line keeps to its own claim's values
            # when the claim could not also keep to the member's plan.
            keep = [i for i, r in enumerate(values) if isinstance(r, dict)
                    and all(_text(r.get(f)) == v for f, v in nearest.items())]
        if not keep:
            return values, weights
        if weights and len(weights) == len(values):
            weights_kept = [weights[i] for i in keep]
        else:
            weights_kept = weights
        return [values[i] for i in keep], weights_kept

    key = (id(values), id(weights), tuple(want.items()), tuple(nearest.items()))
    return _cached(_AGREEING, key, (values, weights), make)


def _text(value: Any) -> str:
    """A field's value as compared: an empty one is the same however held."""
    return "" if value is None else str(value).strip()


@register("lookup")
def lookup(ctx: RowContext) -> Any:
    values: Sequence[Any] = ctx.require("values")
    if not values:
        raise GenerationError(f"{ctx.table}.{ctx.field_name}: lookup has no values")

    weights = ctx.params.get("weights")
    record_key = ctx.params.get("record_key")
    record_field = ctx.params.get("field")

    # Records whose field must be a key of a table: a diagnosis drawn from
    # combinations still has to be a code the diagnosis table holds.
    within = ctx.params.get("within")
    if within and record_field is not None and within in ctx.parent_keys:
        keys = ctx.parent_keys[within]
        signature = within
        if ctx.params.get("filter_attribute"):
            # Narrowed further by an attribute of that table: diagnoses of one
            # chapter, providers of one specialty -- as a reference narrows.
            from sdg.engine.generators.reference import _narrow

            keys = _narrow(ctx, within, keys)
            signature = (within, ctx.params["filter_attribute"],
                         tuple(ctx.params.get("filter_values") or ()))
        values, weights = _within(values, weights, record_field, signature, keys)
        if not values:
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: none of its combinations hold a "
                f"{record_field} that {within} has"
            )

    # Columns that share a record_key share the draw, so they stay consistent.
    rnd = ctx.shared_stream(record_key) if record_key else ctx.stream()

    # Fields of the same records in other columns -- in this row, or in a
    # parent row in another table -- narrow the draw to the records that agree
    # with what they already hold. A claim line's procedure is drawn only from
    # the combinations holding the diagnosis its claim has.
    given = ctx.params.get("given") or {}
    if record_key and any("." in str(ref) for ref in given.values()):
        # Records drawn across tables: only the fields already held by the
        # rows above (the claim, the member) narrow the draw. The other
        # columns of this row draw the same record; narrowing by those
        # already filled would give each column a different list, and so a
        # different record, depending on the order they are filled in.
        given = {other: ref for other, ref in given.items() if "." in str(ref)}
    if given and record_field is not None and record_key:
        # A field drawn beside the others of its record in the same row shares
        # their draw: the record they took is the one this takes. Narrowing
        # the list and drawing again with the same number would land on
        # another record and skew the pairs.
        first = (_weighted(rnd, weights) if weights and len(weights) == len(values)
                 else rnd.below(len(values)))
        rnd = ctx.shared_stream(record_key)
        record = values[first]
        if isinstance(record, dict) and all(
                _text(record.get(other)) == _text(ctx.resolve_name(ref))
                for other, ref in given.items()
                if other != record_field and ctx.can_resolve(ref)
                and ctx.resolve_name(ref) is not None):
            given = {}
        else:
            # Drawn among the records that agree, with a number of its own
            # (the shared one already picked a record these do not include),
            # the same for every column of the record so they stay together.
            rnd = ctx.shared_stream(record_key, salt="agreeing")
    if given and record_field is not None:
        want = {}
        for other_field, reference in given.items():
            if other_field == record_field or not ctx.can_resolve(reference):
                continue
            held = ctx.resolve_name(reference)
            if held is not None:
                want[other_field] = _text(held)
        if want:
            nearest = {f: v for f, v in want.items()
                       if str(given[f]).rpartition(".")[0] == ctx.driving_parent}
            values, weights = _agreeing(values, weights, want, nearest)

    if weights:
        if len(weights) != len(values):
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: {len(values)} values but {len(weights)} weights"
            )
        index = _weighted(rnd, weights)
    else:
        index = rnd.below(len(values))

    chosen = values[index]
    if record_field is None:
        return chosen
    if not isinstance(chosen, dict):
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: field {record_field!r} was asked for "
            f"but the value is not a record"
        )
    if record_field not in chosen:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: record has no key {record_field!r}; "
            f"keys are {sorted(chosen)}"
        )
    return chosen[record_field]
