"""The central generation-rule repository.

One rule per field, shared by everything: the Layout Builder assigns them to
layout fields, and generation materializes a column for each active rule. A
change here reaches every layout on the next generate, because layouts store
rule ids and resolve them at generate time rather than copying the values.

Retiring a rule deactivates it rather than deleting it, so a layout that still
points at it keeps a resolvable reference.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sdg.analyzer.services import layout_service as ls
from sdg.discovery.errors import plain

router = APIRouter(prefix="/rules", tags=["rules"])

log = logging.getLogger(__name__)


@router.get("")
def list_rules(include_inactive: bool = True):
    """Every rule, with the layouts that reference each one."""
    try:
        rules = ls.get_all_rules() if include_inactive else ls.get_rules()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")

    # Refused rather than shown empty: a rule listed as used by nothing may
    # be edited or retired while layouts still depend on it.
    try:
        usage = ls.rule_usage()
        aliases = ls.aliases_by_rule()
    except Exception as e:
        log.exception("Rule usage or aliases could not be read")
        raise HTTPException(status_code=500, detail=(
            f"The layouts and column names that use each rule could not be read. "
            f"Try again when they can be read: {plain(e)}"))

    for r in rules:
        r["used_by"] = usage.get(int(r["id"]), [])
        # The other column names that mean this element.
        r["aliases"] = aliases.get(int(r["id"]), [])

    return {
        "rules": rules,
        "n_active": sum(1 for r in rules if r.get("active", True)),
        "lookups": _lookups(),
        "methods": ls.METHOD_HELP,
    }


class AliasBody(BaseModel):
    column_name: str


@router.post("/{rule_id}/aliases")
def add_alias(rule_id: int, req: AliasBody):
    """Point another column name at this element.

    One element, many names: MEME_FIRST_NAME and SBSB_FIRST_NAME are both a
    first name, and a customer layout will call it FIRST_NM. A column name can
    only ever mean one element — claiming one that is already spoken for is
    refused rather than silently repointed, because the loser of that race would
    start producing another element's data.
    """
    try:
        return ls.add_alias(rule_id, req.column_name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.delete("/aliases/{column_name}")
def drop_alias(column_name: str):
    """Stop a column name meaning anything. The element itself is untouched."""
    try:
        out = ls.remove_alias(column_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")
    if not out["removed"]:
        raise HTTPException(status_code=404, detail=f"No alias “{column_name}”.")
    return out


def _lookups() -> list[str]:
    """Lookup sources a rule can draw from. Empty if SQL Server is unreachable —
    the form still works, the dropdown is just not pre-filled."""
    try:
        from sdg.analyzer.generation_rules import get_available_lookups
        return get_available_lookups()
    except Exception:
        return []


class RuleBody(BaseModel):
    field_name: str
    field_label: str | None = None
    field_category: str = "custom"
    element: str | None = None
    gen_method: str
    data_type: str = "text"
    length: int | None = None
    prefix: str | None = None
    postfix: str | None = None
    start_value: int | None = None
    domain: str | None = None
    format_pattern: str | None = None
    lookup_source: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    decimals: int | None = None
    weights: str | None = None
    distribution: str | None = None
    mean_value: float | None = None
    stddev_value: float | None = None


@router.post("")
def create(req: RuleBody):
    if not req.field_name.strip():
        raise HTTPException(status_code=422, detail="A field name is required.")
    try:
        rule_id = ls.create_rule(req.model_dump())
    except Exception as e:
        detail = str(e)
        if "UNIQUE" in detail.upper() or "duplicate" in detail.lower():
            raise HTTPException(
                status_code=422,
                detail=f"A rule for {req.field_name} already exists. Edit that one instead.",
            )
        raise HTTPException(status_code=500, detail=f"Rule create failed: {detail}")
    return {"id": rule_id, **req.model_dump()}


class RuleUpdate(BaseModel):
    field_label: str | None = None
    field_category: str | None = None
    # What the rule produces, as the catalog names it. A config refers to a
    # rule by this, so being unable to set it meant a config could name
    # something nothing would ever answer to.
    element: str | None = None
    gen_method: str | None = None
    data_type: str | None = None
    length: int | None = None
    prefix: str | None = None
    postfix: str | None = None
    start_value: int | None = None
    domain: str | None = None
    format_pattern: str | None = None
    lookup_source: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    decimals: int | None = None
    weights: str | None = None
    distribution: str | None = None
    mean_value: float | None = None
    stddev_value: float | None = None


@router.put("/{rule_id}")
def update(rule_id: int, req: RuleUpdate):
    changes = {k: v for k, v in req.model_dump(exclude_unset=True).items()}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to update.")
    try:
        return ls.update_rule(rule_id, changes)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rule update failed: {plain(e)}")


class SampleBody(RuleUpdate):
    field_name: str = "SAMPLE"
    rows: int = 5


@router.post("/sample")
def sample(req: SampleBody):
    """A few values of a rule as it stands, made the way a file makes them.

    Each row is drawn with the other rules' values beside it, so a template
    or a combination is shown as it will come out; a rule that cannot make a
    value says why.
    """
    from sdg.engine.generators import GenerationError
    from sdg.engine.rules import RuleError, run

    rule = req.model_dump(exclude={"rows"})
    field = str(rule.get("element") or rule["field_name"]).strip().upper()
    try:
        others = {e: r for e, r in ls.rules_by_element().items() if e != field}
    except Exception:
        # A preview still shows the rule's own values, without the others'.
        log.warning("Other rules could not be read for a rule sample", exc_info=True)
        others = {}
    values = []
    try:
        for row in range(1, max(1, min(req.rows, 20)) + 1):
            context: dict = {}
            for element, other in others.items():
                if str(other.get("gen_method") or "").lower() == "formatted":
                    continue
                try:
                    context[element] = run(other, seed="sample", row_index=row,
                                           field=element, context=context)
                except (RuleError, GenerationError):
                    continue
            values.append(run(rule, seed="sample", row_index=row, field=field,
                              context=context))
    except (RuleError, GenerationError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"values": values}


class ActiveBody(BaseModel):
    active: bool


@router.post("/{rule_id}/active")
def set_active(rule_id: int, req: ActiveBody):
    """Retire or restore a rule. Not a delete — layouts may still reference it."""
    try:
        return ls.set_rule_active(rule_id, req.active)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


# ── Seeding from the plan ────────────────────────────────────────────────────
#
# The one place the two halves of this tool meet on rules. Everything above
# edits the repository; this fills it from the plan, so that an installation
# that has never written a rule by hand still describes its own run.

@router.get("/seed")
def seed_preview():
    """What seeding would add, written nowhere."""
    from sdg.analyzer.services import seed_elements

    try:
        return seed_elements.plan()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


class SeedBody(BaseModel):
    created_by: str = "seed"


@router.post("/seed")
def seed_apply(req: SeedBody | None = None):
    """Add a rule for every generated column that has none.

    Idempotent: a column with a rule is left exactly as it is, so this can be
    run again after the plan grows a table without disturbing anything a
    person has edited since.
    """
    from sdg.analyzer.services import seed_elements

    try:
        out = seed_elements.apply(created_by=(req.created_by if req else "seed"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")
    if out["failed"]:
        # Partial success is still success: what was added stays added, and
        # the caller is told exactly what did not.
        return {**out, "ok": False}
    return {**out, "ok": True}
