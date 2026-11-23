"""Reference generator.

Points a child row at a real parent key. Two ways to pick:

driving  take the key of the parent row that drove this row into existence.
         This is the parent the ratio counted against.
hash     pick from the parent's key set with hash(seed, row) modulo its size,
         which is the rule the build brief fixes for reference sets.

A hash pick can be narrowed three ways. `keys` restricts it to a stated set,
which is how a discovered pattern says "these ten diagnoses, in these
proportions" without having to become a different kind of rule. The other two
both use `filter_attribute`, naming a column of the parent:

match_field     the attribute must equal a value in *this* row, which is how a
                claim finds a provider in the member's own state.
filter_values   the attribute must be one of a stated set, which is how a
                pattern says "providers in this specialty" when the specialty
                is a column of the provider table rather than of the claim.

The two differ in what an empty result means. A row-matched filter that finds
nothing is a data condition -- this member's state has no providers -- and the
run continues against the full set, with the validator reporting the referential
outcome. A stated filter that finds nothing means the pattern named something
the reference set does not contain, which is a mistranslation between the
analyzer's vocabulary and the database's, and it stops the run rather than
quietly generating the opposite of what was asked for.
"""

from __future__ import annotations

from typing import Any

from sdg.engine.generators.base import GenerationError, RowContext, register


@register("reference")
def reference(ctx: RowContext) -> Any:
    parent_table = ctx.require("parent_table")
    parent_column = ctx.require("parent_column")
    pick = ctx.params.get("pick", "hash")

    if pick == "driving":
        parent = ctx.parents.get(parent_table)
        if parent is None:
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: no driving parent row from {parent_table}; "
                f"available: {sorted(ctx.parents)}"
            )
        if parent_column not in parent:
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: {parent_table} has no column {parent_column!r}"
            )
        return parent[parent_column]

    # A stated key set replaces the parent's own, because the point of stating
    # it is to draw from fewer rows than exist. Checked before the parent keys
    # are read: a pattern that names its codes does not need them.
    stated = ctx.params.get("keys")
    if stated:
        weights = ctx.params.get("weights")
        stream = ctx.stream()
        if weights:
            if len(weights) != len(stated):
                raise GenerationError(
                    f"{ctx.table}.{ctx.field_name}: {len(stated)} keys but "
                    f"{len(weights)} weights"
                )
            return stated[stream.weighted_index(weights)]
        return stated[stream.below(len(stated))]

    keys = ctx.parent_keys.get(parent_table)
    if not keys:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: no keys loaded for {parent_table}. "
            f"Either it generates no rows or its reference set is empty."
        )

    candidates = _narrow(ctx, parent_table, keys)
    return candidates[ctx.stream().below(len(candidates))]


def _narrow(ctx: RowContext, parent_table: str, keys: list[Any]) -> list[Any]:
    """Restrict the key set to parents whose attribute matches."""
    attribute = ctx.params.get("filter_attribute")
    if not attribute:
        return keys

    match_field = ctx.params.get("match_field")
    stated = ctx.params.get("filter_values")
    if not match_field and not stated:
        return keys

    attributes = ctx.parent_attributes.get(parent_table)
    if not attributes:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: {parent_table} was not loaded with "
            f"attribute {attribute!r}, so it cannot be filtered"
        )

    if stated:
        # Compared as text because a reference set reads whatever the column's
        # type gives and a pattern states strings; trimmed, because a code
        # read from a wider char column carries padding the set's own does not.
        wanted_set = {str(v).strip() for v in stated}
        narrowed = [
            k for k in keys
            if str(attributes.get(k, {}).get(attribute)).strip() in wanted_set
        ]
        if not narrowed:
            held = sorted({
                str(row.get(attribute)) for row in attributes.values()
                if row.get(attribute) is not None
            })
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: no row of {parent_table} has "
                f"{attribute} in {sorted(wanted_set)}. The set holds "
                f"{', '.join(held[:12]) or 'nothing'}"
                f"{' and more' if len(held) > 12 else ''}"
            )
        return narrowed

    wanted = ctx.resolve_name(match_field)
    narrowed = [k for k in keys if attributes.get(k, {}).get(attribute) == wanted]
    # A row-matched filter that matches nothing falls back to the full set
    # rather than failing the run; the post-run validator reports the
    # referential result.
    return narrowed or keys
