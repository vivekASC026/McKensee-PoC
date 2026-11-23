"""Give every generated element a rule in the generation-rule repository.

    python -m sdg.analyzer.services.seed_elements            # what would change
    python -m sdg.analyzer.services.seed_elements --apply    # write it

The repository is the vocabulary a layout field is bound to: upload a customer
layout, and each of its fields is matched to a rule by column name. An empty
repository means every field falls to `default`, which writes the column blank.
Nothing in the interface could create a rule, so the repository stayed empty
and the layout feature could not do its job.

The columns come from the plan this tool has already built -- every field of
every table the plan marks `generate`. That is the right source: it is exactly
the set of columns a run writes, it already carries the element each column was
matched to, and it moves when the plan moves. (The analyzer seeded from its own
generation router, which this project replaced with the plan engine.)

Each column is seeded with the rule method closest to the generator the plan
chose, so a seeded repository describes the run that would actually happen
rather than a wall of blanks.

Safe to re-run. A rule is named by its element; a repository from before that
is re-keyed first (see `rekey`). Otherwise existing rules are never modified;
only missing ones are added.
"""

from __future__ import annotations

import logging
import sys

from sdg.packs import held

log = logging.getLogger(__name__)

# Readable names for the FACETS columns. Anything not listed falls back to a
# title-cased version of the column name, which is serviceable for the long tail
# of MCTR/NVL codes nobody reads directly.
LABELS = held("features:analyzer.seed_element_labels")

# Which bucket a column belongs to, by prefix. Only used to group the screen.
CATEGORIES = (
    ("CLCL_", "claim"),
    ("CDML_", "claim line"),
    ("PRPR_", "provider"),
    ("IDCD_", "diagnosis"),
    ("IPCD_", "procedure"),
    ("MEME_", "member"),
    ("SBSB_", "subscriber"),
    ("SBSG_", "enrollment"),
    ("SGSG_", "section"),
    ("GRGR_", "group"),
    ("CSPI_", "plan"),
    ("CSPD_", "plan"),
    ("PDPD_", "product"),
    ("PLDS_", "plan"),
    ("PDDS_", "product"),
)


def category_for(column: str) -> str:
    for prefix, cat in CATEGORIES:
        if column.startswith(prefix):
            return cat
    return "reference"


# What a plan generator means in the rule vocabulary. The two were designed
# apart and neither is a superset, so this is a reading of one in the other:
#
#   sequence       a counter            -> sequential
#   number         a drawn number       -> random
#   lookup         a reference list     -> lookup
#   reference      a key from a parent  -> lookup, from the parent's list
#   date_relative  offset from a date   -> formatted, derived from the row
#   expression     computed from fields -> formatted, likewise
#   constant       the same every row   -> constant
#   pattern        carried from data    -> pattern
#
# `skip` has no reading because the column is not written.
METHOD_FOR_GENERATOR = {
    "sequence": "sequential",
    "number": "range",
    "lookup": "lookup",
    "reference": "lookup",
    "date_relative": "date",
    "expression": "formatted",
    "constant": "constant",
    "pattern": "pattern",
}


def _plan():
    """The plan as the service holds it, loaded late to keep imports cheap."""
    from sdg.api.app import service

    return service.require_plan()


def generated_columns(plan_obj=None) -> list[dict]:
    """Every element a run writes, in plan order, with what the plan says of it.

    One entry per element, not per column: the repository is keyed on the
    element, because that is what a rule produces and what every config names.
    A first name on the subscriber and on the member is one element produced
    twice. The field that makes the value wins over one that copies or sums
    it -- a claim line's CLAIM_ID is its claim's, and the claim's is where the
    format of one is measured. Among equals, the first in the cascade. A column
    bound to no element has no rule.
    """
    plan_obj = plan_obj if plan_obj is not None else _plan()

    chosen: dict[str, tuple] = {}
    for table in plan_obj.tables:
        if table.mode != "generate":
            continue
        for field in table.fields:
            if field.generator == "skip":
                continue
            if not field.element:
                # A rule is an element. A column nobody has said the meaning
                # of is a question for Review, not a rule.
                continue
            key = field.element.strip().upper()
            held = chosen.get(key)
            if held is None or (_derived(held[1]) and not _derived(field)):
                chosen[key] = (table, field)

    out: list[dict] = []
    for key, (table, field) in chosen.items():
        out.append({
            "name": key,
            "column": field.name,
            "table": table.name,
            "element": field.element,
            "generator": field.generator,
            "method": METHOD_FOR_GENERATOR.get(field.generator, "pattern"),
            "data_type": field.data_type or "text",
            "label": key.replace("_", " ").title(),
            "category": category_for(field.name),
            # What the plan measured, not only what kind of thing it is.
            # Without this a rule knows it is a pattern and not which one.
            **_parameters(field),
            **_reference_values(field, plan_obj),
            **_total(field, plan_obj),
        })
    return out


def _total(field, plan_obj) -> dict:
    """A total of a child column, as the element it totals: sum:CHARGE_AMOUNT.

    The plan sums a claim's lines by table and column; a rule has neither, so
    it names the element the lines carry, and a file totals a claim's lines of
    that element.
    """
    import re

    if field.generator != "expression":
        return {}
    found = re.fullmatch(r"\s*sum\(\s*([\w.]+)\.(\w+)\s*\)\s*",
                         str((field.params or {}).get("expression") or ""))
    if not found:
        return {}
    table, column = found.groups()
    child = next((f for t in plan_obj.tables if t.name == table
                  for f in t.fields if f.name == column), None)
    if child is None or not child.element:
        return {}
    return {"format_pattern": f"sum:{child.element.strip().upper()}"}


# A reference set larger than this is a population, not a vocabulary, and a
# rule listing it would be a copy of a table.
REFERENCE_LIMIT = 500


def _reference_values(field, plan_obj) -> dict:
    """The keys a reference points at, where they are a short list.

    A coverage plan, a product, a group: the rule for one is "one of these",
    and these are the rows of a reference table. Read once, when the rule is
    seeded, so producing a value never needs the database.
    """
    params = dict(field.params or {})
    if field.generator != "reference" or not params.get("parent_table"):
        return {}
    parent = next((t for t in plan_obj.tables if t.name == params["parent_table"]), None)
    if parent is None or parent.mode != "reuse":
        return {}
    column = params.get("parent_column")
    try:
        from sdg.api.app import service

        rows = service.registry.query(
            "source",
            f"SELECT DISTINCT TOP ({REFERENCE_LIMIT + 1}) [{column}] AS v "
            f"FROM {parent.name} WHERE [{column}] IS NOT NULL ORDER BY [{column}]",
        )
    except Exception as exc:
        # Not "no short list": the rule would lose its domain without a word.
        # Recorded on the column, so `apply` reports it and `_refresh` keeps
        # the domain a rule already has rather than clearing it.
        log.warning("Could not read the reference values of %s.%s",
                    parent.name, column, exc_info=True)
        return {"reference_error": f"{parent.name}.{column}: {type(exc).__name__}"}
    values = [str(r["v"]).strip() for r in rows if str(r["v"]).strip()]
    if not values or len(values) > REFERENCE_LIMIT:
        return {}
    return {"domain": ",".join(values), "lookup_source": f"{parent.name}.{column}"}


def _is_day(text: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(text[:10])
        return True
    except ValueError:
        return False


def _derived(field) -> bool:
    """Whether a field takes its value from another rather than making one."""
    return field.generator in ("reference", "expression")


def _parameters(field) -> dict:
    """The plan's own parameters, in the shape a rule records them.

    The two vocabularies were designed apart. A plan field holds a generator
    and its params; a rule holds a method and a handful of named columns. This
    is the reading of one in the other, and it is deliberately lossy in one
    direction only -- anything a rule has no column for is left behind rather
    than encoded into a column that means something else.
    """
    params = dict(field.params or {})
    out: dict = {}

    if regex := params.get("regex"):
        out["format_pattern"] = str(regex)
    if values := params.get("values"):
        # A rule stores its list as text, because the repository is a table and
        # the alternative is a second one.
        plain = [str(v) for v in values if not isinstance(v, dict)]
        if plain:
            out["domain"] = ",".join(plain)
            weights = params.get("weights") or []
            if len(weights) == len(plain):
                out["weights"] = ",".join(str(w) for w in weights)
    if (start := params.get("start")) is not None:
        out["start_value"] = int(start)
    if (low := params.get("min")) is not None and params.get("max") is not None:
        # The range it was measured in, not only how wide it is: units seen
        # from 1 to 10 are not "a two-digit number".
        out["min_value"] = float(low)
        out["max_value"] = float(params["max"])
        out["decimals"] = int(params.get("decimals") or 0)
        # And how the values fall between them. Most charges are near a
        # hundred and a few reach 25,000; drawn evenly over that range the
        # average would be 12,500, so the shape travels with the bounds.
        if params.get("distribution") in ("normal", "lognormal")                 and params.get("mean") is not None and params.get("stddev") is not None:
            out["distribution"] = str(params["distribution"])
            out["mean_value"] = float(params["mean"])
            out["stddev_value"] = float(params["stddev"])
    if anchor := params.get("anchor"):
        # The day the window is measured from, and how far back it reaches.
        # Both are the plan's measurement; a date rule without them would have
        # to invent a range, which is what "sometime in the last ten years"
        # means when nobody measured anything.
        if _is_day(str(anchor)):
            out["format_pattern"] = str(anchor)
            # Days either side of the anchor, as the plan draws them: a window
            # can reach forward (service dates after 2020-01-01) as well as
            # back (birth dates before today).
            low, high = params.get("min_days"), params.get("max_days")
            if low is not None and high is not None:
                out["min_value"], out["max_value"] = int(low), int(high)
                out["start_value"] = int(abs(int(low)))
        else:
            # Anchored to another column -- a service date after the member's
            # effective date. A rule has no column to read, so it keeps the
            # span the plan allows, back from the latest day it allows.
            out["format_pattern"] = str(params.get("max_date") or "") or None
            span = params.get("max_days")
            if span is not None:
                out["start_value"] = int(abs(int(span)))
                out["min_value"], out["max_value"] = -int(abs(int(span))), 0
    if source := params.get("record_key"):
        out["lookup_source"] = str(source)
    return out


def plan(plan_obj=None) -> dict:
    """What seeding would add, without writing anything. With no plan -- a
    database just read has none until one is built -- nothing would be."""
    from sdg.analyzer.services import layout_service as ls

    existing = {str(r["field_name"]).strip().upper() for r in ls.get_all_rules()}
    if plan_obj is None:
        from sdg.api.app import service

        if service.plan is None:
            return {"has_plan": False, "columns": [], "existing": sorted(existing),
                    "missing": [], "n_columns": 0, "n_existing": len(existing),
                    "n_missing": 0}
    columns = generated_columns(plan_obj)
    missing = [c for c in columns if c["name"] not in existing]
    return {
        "has_plan": True,
        "columns": columns,
        "existing": sorted(existing),
        "missing": missing,
        "n_columns": len(columns),
        "n_existing": len(existing),
        "n_missing": len(missing),
    }


def apply(created_by: str = "seed", plan_obj=None) -> dict:
    from sdg.analyzer.services import layout_service as ls

    rekey()
    found = plan(plan_obj)
    refreshed = _refresh(found["columns"])
    added, failed = [], []
    for column in found["columns"]:
        if column.get("reference_error"):
            failed.append(f"{column['column']}: reference values could not be read "
                          f"({column['reference_error']}). Its domain was not updated; "
                          f"seed again once the source can be read.")
    for column in found["missing"]:
        try:
            rule_id = ls.create_rule({
                "field_name": column["name"],
                "field_label": column["label"],
                "field_category": column["category"],
                # What the value is, as the catalog names it. The column says
                # where it lands; this says what it means, and it is what a
                # config refers to.
                "element": column["element"],
                "gen_method": column["method"],
                "data_type": column["data_type"],
                "prefix": None, "postfix": None,
                "length": column.get("length"),
                "start_value": column.get("start_value"),
                "domain": column.get("domain"),
                "format_pattern": column.get("format_pattern"),
                "lookup_source": column.get("lookup_source"),
                **{k: column.get(k) for k in MEASURES},
            }, created_by=created_by)
            # The column it was seeded from still means this element, so a
            # sample layout naming the column resolves to it.
            if column["column"].strip().upper() != column["name"]:
                _alias(rule_id, column["column"])
            added.append(column["name"])
        except Exception as e:
            failed.append(f"{column['column']}: {type(e).__name__}: {e}")
    return {"added": added, "refreshed": refreshed, "failed": failed, **found}


# What a seeded rule takes from the plan, beyond its method.
MEASURES = (
    "min_value", "max_value", "decimals", "weights",
    "distribution", "mean_value", "stddev_value",
)
SEEDED_BY = ("seed", "rekey", "system")


def _refresh(columns: list[dict]) -> list[str]:
    """Bring rules nobody has edited up to what the plan now measures.

    A rule a person changed is theirs and is left alone; one the seeding wrote
    describes the plan, and a plan rebuilt since then measures something else.
    """
    from sdg.analyzer.services import layout_service as ls

    by_name = {str(r["field_name"]).strip().upper(): r for r in ls.get_all_rules()}
    refreshed = []
    for column in columns:
        rule = by_name.get(column["name"])
        if rule is None or str(rule.get("updated_by") or "") not in SEEDED_BY:
            continue
        wanted = {
            "gen_method": column["method"],
            "data_type": column["data_type"],
            "length": column.get("length"),
            "start_value": column.get("start_value"),
            "domain": column.get("domain"),
            "format_pattern": column.get("format_pattern"),
            "lookup_source": column.get("lookup_source"),
            **{k: column.get(k) for k in MEASURES},
        }
        if column.get("reference_error"):
            # The reference could not be read this time: what the rule holds
            # is kept, not replaced by the empty result of a failed read.
            wanted.pop("domain")
            wanted.pop("lookup_source")
        changes = {k: v for k, v in wanted.items() if rule.get(k) != v}
        if changes:
            ls.update_rule(rule["id"], changes, updated_by="seed")
            refreshed.append(column["name"])
    return refreshed


def rekey(updated_by: str = "rekey") -> dict:
    """Name every rule by the element it produces, once.

    The repository used to be keyed on column, so DOB was MEME_BIRTH_DT. Each
    element keeps one rule, named for it. The column names become aliases of
    that rule -- a sample layout naming SBSB_ID still resolves -- and rules
    made redundant are retired, not deleted, with any layout field that
    pointed at one moved to the rule that remains.
    """
    from sdg.analyzer.services import layout_service as ls

    rules = ls.get_all_rules()
    by_name = {str(r["field_name"]).strip().upper(): r for r in rules}
    groups: dict[str, list[dict]] = {}
    for rule in rules:
        element = str(rule.get("element") or "").strip().upper()
        if element:
            groups.setdefault(element, []).append(rule)

    renamed, retired, moved = [], [], {}
    for element, members in groups.items():
        members.sort(key=lambda r: (not r["active"], r["id"]))
        keeper = by_name.get(element) or members[0]
        if str(keeper["field_name"]).strip().upper() != element:
            old = str(keeper["field_name"])
            _rename(keeper["id"], element)
            renamed.append(f"{old} -> {element}")
            _alias(keeper["id"], old)
        for other in members:
            if other["id"] == keeper["id"]:
                continue
            moved[other["id"]] = keeper["id"]
            if other["active"]:
                ls.set_rule_active(other["id"], False, updated_by=updated_by)
                retired.append(str(other["field_name"]))
            _alias(keeper["id"], str(other["field_name"]))

    if moved:
        _repoint_layouts(moved)
    return {"renamed": renamed, "retired": retired, "repointed": len(moved)}


def _rename(rule_id: int, name: str) -> None:
    from datetime import datetime

    from sdg.analyzer.services import layout_service as ls

    conn = ls._conn()
    try:
        conn.cursor().execute(
            f"UPDATE {ls.RULES_TABLE} SET field_name = ?, updated_by = 'rekey', "
            f"updated_at = ? WHERE id = ?", name, datetime.now(), rule_id)
        conn.commit()
    finally:
        conn.close()


def _alias(rule_id: int, column: str) -> None:
    """Point a column name at a rule, moving it if it pointed elsewhere."""
    from sdg.analyzer.services import layout_service as ls

    ls.ensure_alias_table()
    conn = ls._conn()
    try:
        conn.cursor().execute(
            f"DELETE FROM {ls.ALIASES_TABLE} WHERE UPPER(column_name) = ?",
            column.strip().upper())
        conn.commit()
    finally:
        conn.close()
    try:
        ls.add_alias(rule_id, column, created_by="rekey")
    except ValueError:
        pass


def _repoint_layouts(moved: dict[int, int]) -> None:
    import json

    from sdg.analyzer.services import layout_service as ls

    ls.ensure_table()
    conn = ls._conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id, field_spec FROM {ls.LAYOUTS_TABLE}")
        for layout_id, spec in cur.fetchall():
            fields = json.loads(spec) if spec else []
            changed = False
            for f in fields:
                if f.get("rule_id") in moved:
                    f["rule_id"] = moved[f["rule_id"]]
                    changed = True
            if changed:
                # The same field still resolves to the same element, so what
                # an approval attested to has not changed and it stands.
                cur.execute(f"UPDATE {ls.LAYOUTS_TABLE} SET field_spec = ? WHERE id = ?",
                            json.dumps(fields), layout_id)
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    if "--apply" not in argv:
        found = plan()
        print(f"\n{found['n_columns']} generated columns, "
              f"{found['n_existing']} rules exist, "
              f"{found['n_missing']} would be added:\n")
        for c in found["missing"]:
            print(f"  {c['name']:<24} {c['label']:<32} "
                  f"{c['category']:<12} {c['method']}")
        print("\nRe-run with --apply to write them.\n")
        return 0

    out = apply()
    print(f"\nadded {len(out['added'])} elements")
    if out["failed"]:
        print(f"{len(out['failed'])} failed:")
        for f in out["failed"]:
            print(f"  {f}")
    print()
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
