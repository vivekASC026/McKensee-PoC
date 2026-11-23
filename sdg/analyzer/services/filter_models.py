"""The filters an analysis takes, as the business pack lists them.

Each record kind's loader in features.json (loaders.<entity>.filters) says
which filters it takes and how each reaches SQL (entity_loader.where). The
request models are made from that list when the server starts, so a pack's
filters are the fields the screens send -- health's group, plan category,
plan and product; a bank's branch and product -- with no field written here.

A filter of ids takes numbers, one of values takes text, a bound takes one
number; each list filter also takes one bare value, as the loader does.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional, Union

from pydantic import BaseModel, Field, create_model

from sdg import packs
from sdg.packs import held

_TYPES: dict[str, Any] = {
    "ids": Optional[Union[list[int], int]],
    "values": Optional[Union[list[str], str]],
    "at_least": Optional[int],
    "at_most": Optional[int],
}

# The filter a child analysis is narrowed to one type of record by (health:
# claim_type), where the pack has one.
TYPE_FILTER: Optional[str] = held("features:store.type_filter")


def listed(entity: str | None) -> list[dict]:
    """The filters a record kind's loader takes, in the pack's order."""
    loaders = packs.active().features.get("loaders") or {}
    return list((loaders.get(entity or "") or {}).get("filters") or [])


class AnalysisFilters(BaseModel):
    """Filters for one record kind: its loader's filters and a defined
    pattern's rules beyond them ({feature: {"in": [...]}} or
    {feature: {"min": x, "max": y}})."""

    entity_key: ClassVar[str] = ""
    filter_keys: ClassVar[tuple[str, ...]] = ()

    conditions: Optional[dict[str, Any]] = None

    def as_kwargs(self) -> dict:
        """Only the filters given -- none, an empty list or blank is not one."""
        from sdg.analyzer.services.defined_patterns import check

        out = {}
        for key in self.filter_keys:
            value = getattr(self, key)
            if value is None or value == "" or value == []:
                continue
            out[key] = value
        conditions = check(self.entity_key, self.conditions)
        if conditions:
            out["conditions"] = conditions
        return out


def model(name: str, entity: str | None, base: type[AnalysisFilters] = AnalysisFilters,
          **extra: Any) -> type[AnalysisFilters]:
    """A request model of `entity`'s filters, with any `extra` fields."""
    filters = listed(entity)
    fields = {f["key"]: (_TYPES[f["kind"]], None) for f in filters}
    made = create_model(name, __base__=base, **fields, **extra)
    made.entity_key = entity or ""
    made.filter_keys = tuple(f["key"] for f in filters)
    return made


def of(model_: type[AnalysisFilters], filters: dict | None, record_type: str | None = None,
       **extra: Any) -> dict:
    """Stored or sent filters, read as `model_` reads them: only the filters
    its record kind takes, and the record type where one is chosen."""
    filters = filters or {}
    given = {k: filters[k] for k in (*model_.filter_keys, "conditions") if k in filters}
    if record_type and TYPE_FILTER and TYPE_FILTER in model_.filter_keys:
        given[TYPE_FILTER] = [record_type]
    return model_(**given, **extra).as_kwargs()


K_FIELD = (Optional[int], Field(None, ge=1, le=20,
                                description="None selects K by silhouette score; 1 is the whole scope"))
