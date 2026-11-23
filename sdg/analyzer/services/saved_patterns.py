"""Keeping a pattern, so it is ready the next time it is wanted.

A pattern is a saved list of elements and their values. Saving one stores what
it says -- its ages, its mix of statuses, its diagnoses -- and the members it
holds, so that a member file made from it next week carries the same values and
a claim file is still made for the same people, whatever a fresh clustering of
the same data would split differently by then.

A pattern can be the whole of what an analysis found, or only the records a
person chose from underneath it. Either way it is described the same way the
analysis describes one, from the rows themselves.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import numpy as np
import pandas as pd

from sdg import packs
from sdg.packs import held, is_child

# How a record is named between the screen and the server: a primary record
# by its key, a child record's line by the child's key and the line's number
# (the child loader's order: health's claim and line, a bank's account and
# transaction).
MEMBER_KEY = held("features:store.member_key")


def row_keys(frame: pd.DataFrame, entity: str) -> list[str]:
    """One stable name per row, the same on every page and every rebuild."""
    if frame.empty:
        return []
    from sdg.analyzer import entity_loader

    order = entity_loader.config(entity).get("order") or [] if is_child(entity) else []
    if len(order) == 2 and set(order) <= set(frame.columns):
        record, line = order
        return [f"{str(c).strip()}-{int(s)}" for c, s in
                zip(frame[record].tolist(), frame[line].tolist())]
    if MEMBER_KEY in frame.columns:
        return [str(int(v)) if pd.notna(v) else "" for v in frame[MEMBER_KEY].tolist()]
    return [str(i) for i in range(len(frame))]


def _claims_filters(filters: dict, claim_type: Optional[str]) -> dict:
    """A child analysis's filters, as its request model reads them."""
    from sdg.analyzer.services import filter_models
    from sdg.api.routers.claims import ChildAnalyzeRequest

    return filter_models.of(ChildAnalyzeRequest, filters, claim_type)


def _member_filters(filters: dict) -> dict:
    """A primary analysis's filters, as its request model reads them."""
    from sdg.analyzer.services import filter_models
    from sdg.analyzer.services.schemas import Filters

    return filter_models.of(Filters, filters)


def rows(entity: str, filters: dict, k: Optional[int], cluster_id: int,
         claim_type: Optional[str] = None) -> pd.DataFrame:
    """Every source row of an analyzed pattern, from the session's working files."""
    from . import pattern_data

    scope = _claims_filters(filters, claim_type) if is_child(entity) else _member_filters(filters)
    return pattern_data.rows_for_live(entity, scope, k, cluster_id)["df"]


def _profile(entity: str, frame: pd.DataFrame, filters: dict, cluster_id: int,
             claim_type: Optional[str]) -> tuple[dict, str]:
    """The pattern the rows describe, in the shape an analysis gives one."""
    if is_child(entity):
        from . import claims_service as cs

        lines = len(frame)
        claims = (int(frame[cs.RECORD_KEY].nunique()) if cs.RECORD_KEY in frame.columns
                  else lines)
        described = cs.describe(frame, cluster_id, total_claims=claims,
                                total_lines=lines, scope_n=claims)
        described["claims_pattern"] = True
        return described, described["name"]

    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE
    from sdg.analyzer.naming import build_contextual_name
    from sdg.analyzer.profiler import profile_cluster

    from . import service

    scope = _member_filters(filters)
    members, _labels, family, used = service._load(scope, DEFAULT_REFERENCE_DATE)
    mask = members[MEMBER_KEY].isin(frame[MEMBER_KEY]).to_numpy()
    profile = profile_cluster(members, family, mask, cluster_id, int(mask.sum()))
    from sdg.plan.combinations import for_patterns, of_rows

    from sdg.analyzer import entity_loader

    profile["combinations"] = of_rows(members[mask], entity_loader.config(service.ENTITY)["table"],
                                      for_patterns())
    labels = service.scope_labels(service.build_subset_summary(members, used))
    name = build_contextual_name(labels, cluster_id=cluster_id, profile=profile)
    return service.to_py(profile), name


def _chosen_rows(entity: str, filters: dict, k: Optional[int], cluster_id: int,
                 claim_type: Optional[str], record_keys: Optional[list[str]]):
    """A pattern's rows -- all of them, or only the chosen records -- and the
    chosen records' names, sorted."""
    frame = rows(entity, filters, k, cluster_id, claim_type)
    chosen = sorted(set(record_keys or []))
    if chosen:
        wanted = set(chosen)
        keep = np.array([key in wanted for key in row_keys(frame, entity)], dtype=bool)
        frame = frame[keep]
        if frame.empty:
            raise ValueError("None of the chosen records are in this pattern any more.")
    return frame, chosen


def profile_of(entity: str, filters: dict, k: Optional[int], cluster_id: int,
               claim_type: Optional[str] = None,
               record_keys: Optional[list[str]] = None) -> dict:
    """The profile saving these records would store -- their values, not the
    whole pattern's -- measured without saving anything: what a load of
    chosen records sets, shown before Load is pressed."""
    frame, _chosen = _chosen_rows(entity, filters, k, cluster_id, claim_type, record_keys)
    profile, name = _profile(entity, frame, filters, cluster_id, claim_type)
    profile["name"] = name
    return profile


def save(entity: str, filters: dict, k: Optional[int], cluster_id: int,
         claim_type: Optional[str] = None, record_keys: Optional[list[str]] = None,
         name: Optional[str] = None, saved_by: str = "user",
         origin: Optional[str] = None) -> dict:
    """Store a pattern -- whole, or only the chosen records -- and mark it saved.

    Saving the same thing twice finds the same row rather than adding another:
    the key is a digest of what was saved.
    """
    from sdg.analyzer import db

    from .discovery_service import canonical_combo
    from .service import book_size

    frame, chosen = _chosen_rows(entity, filters, k, cluster_id, claim_type, record_keys)
    profile, found_name = _profile(entity, frame, filters, cluster_id, claim_type)
    members = sorted({int(v) for v in frame[MEMBER_KEY].dropna().tolist()})
    title = (name or "").strip() or (
        f"{found_name} · {len(chosen):,} chosen record{'s' if len(chosen) != 1 else ''}"
        if chosen else found_name)

    # Where it came from, so its rows can be shown again, and the members it
    # holds, so a claim file goes to the same people whatever a fresh
    # clustering would do.
    # Named, and given its share of the population, so the same pattern can be
    # resolved onto a plan -- the database route -- exactly as an analyzed one.
    if is_child(entity):
        from .claims_service import book_totals

        whole = book_totals()["claims"] or 1
        share = float(profile.get("n_claims") or 0) / whole
    else:
        share = len(members) / (book_size() or 1)
    profile.update({
        "name": title,
        "pct_of_total": round(share, 6),
        "origin": origin or "discovered",
        "saved_from": {"domain": entity, "filters": filters, "k": k,
                       "cluster_id": cluster_id, "claim_type": claim_type,
                       "records": len(chosen) or None, "record_keys": chosen},
        "member_keys": members,
        "n_members": len(members),
    })

    digest = hashlib.md5(json.dumps(
        [entity, filters, k, cluster_id, claim_type, chosen], sort_keys=True, default=str,
    ).encode()).hexdigest()
    # The scope it was found in: the pack's scope levels of its filters.
    from sdg.analyzer.pattern_scope import SCOPE_KEYS

    combo = canonical_combo({k2: v for k2, v in filters.items() if k2 in SCOPE_KEYS},
                            entity=entity)
    # Saved already: that row is somebody's, and is returned as it is. Saving
    # again neither rewrites it nor makes a second one, and `created` says so,
    # so nothing that saved here mistakes it for a row of its own.
    key = f"saved|{digest}"
    existing = db.pattern_id_for_key(key)
    if existing:
        # Under the name it was saved with: that is the name it is listed by.
        stored = db.get_pattern(int(existing)) or {}
        # Deleted from Saved keeps the row (so Undo can bring it back): saving
        # it again brings it back too. Before, it stayed unsaved -- off the
        # Saved list and under Not saved for good, while Save said "Saved".
        # Still not `created`: the row was here before, and what cleans up
        # after a save removes only rows the save created.
        restored = not stored.get("saved_by")
        if restored:
            db.mark_pattern_saved(int(existing), saved_by)
        return {"pattern_id": int(existing), "created": False, "restored": restored,
                "name": str(stored.get("contextual_name") or title),
                "domain": entity, "member_count": len(members),
                "records": len(chosen) or None,
                "profile": {k: v for k, v in profile.items()
                            if k not in ("member_keys", "saved_from")}}

    book = book_size() or 1
    pattern_id = db.upsert_pattern(
        combo, cluster_id, title, len(members), len(members) / book, 0.0,
        profile, None, pattern_key=key,
    )
    if not pattern_id:
        raise ValueError("The pattern could not be stored.")
    db.mark_pattern_saved(pattern_id, saved_by)
    if origin == "user_defined":
        # Somebody stated it rather than the analyzer finding it; listed so.
        db.mark_pattern_origin(pattern_id, origin, saved_by)
    return {"pattern_id": int(pattern_id), "created": True, "name": title, "domain": entity,
            "member_count": len(members), "records": len(chosen) or None,
            "profile": {k: v for k, v in profile.items()
                        if k not in ("member_keys", "saved_from")}}


def saved_rows(profile: dict[str, Any]) -> Optional[pd.DataFrame]:
    """A saved pattern's rows: the analysis it came from, narrowed to its records."""
    origin = profile.get("saved_from")
    if not isinstance(origin, dict):
        return None
    entity = origin.get("domain") or packs.active().primary
    frame = rows(entity, origin.get("filters") or {}, origin.get("k"),
                 int(origin.get("cluster_id") or 0), origin.get("claim_type"))
    chosen = set(origin.get("record_keys") or [])
    if chosen:
        frame = frame[np.array([k in chosen for k in row_keys(frame, entity)], dtype=bool)]
        if len(frame) < len(chosen):
            # The analysis run again groups the records differently, and the
            # cluster it was saved from no longer holds them all. The records
            # are named, so they are found in the analysis's whole scope.
            scope = _scope_rows(entity, origin.get("filters") or {}, origin.get("claim_type"))
            frame = scope[np.array([k in chosen for k in row_keys(scope, entity)], dtype=bool)]
    return frame


def _scope_rows(entity: str, filters: dict, claim_type: Optional[str]) -> pd.DataFrame:
    """Every row an analysis looked at, before it grouped them."""
    if is_child(entity):
        from . import claims_service as cs

        frame, _used = cs._load(_claims_filters(filters, claim_type))
        return frame
    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE

    from . import service

    return service._load(_member_filters(filters), DEFAULT_REFERENCE_DATE)[0]


def member_keys(profile: dict[str, Any]) -> Optional[list[int]]:
    """The members a saved pattern holds, when it recorded them."""
    keys = profile.get("member_keys")
    return [int(k) for k in keys] if isinstance(keys, list) and keys else None
