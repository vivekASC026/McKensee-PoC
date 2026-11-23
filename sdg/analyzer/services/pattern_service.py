"""User-defined patterns, cloning, and certification.

Three ideas, and the distinctions between them are the point:

  Discovered patterns are evidence. The system found the combination in real
  data. They are immutable — editing one would quietly turn evidence into
  opinion while leaving it labeled as evidence.

  User-defined patterns are hypotheses. An SME states a combination of feature
  values for a scenario the data has not seen yet. Values are deliberately NOT
  validated against reference data: a genuinely new line of business has codes
  that are not configured anywhere, and refusing them would defeat the purpose.
  The tool cannot vouch for these, which is exactly what the flag says.

  Certification closes the loop. When discovery later finds the same
  combination, the two are offered for merge. A reviewer approves it — never
  the system on its own, because silently promoting a hypothesis to evidence
  would rewrite the provenance of data already generated under it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional

from sdg.analyzer import db
from sdg.analyzer.config import SQL_SCHEMA
from sdg.analyzer import pattern_scope as scope
from sdg.packs import held

PATTERNS = f"{SQL_SCHEMA}.patterns"

DISCOVERED = "discovered"
USER_DEFINED = "user_defined"

# The features a pattern is defined by. Fixed on purpose: a user-defined
# pattern varies the *values*, never the feature set, so a stated pattern stays
# structurally identical to a discovered one — which is what makes certification
# a like-for-like comparison rather than a guess.
FEATURES = tuple(held("features:patterns.scope_levels"))

# Cluster id for user-defined patterns. They come from no clustering run, and
# reusing 0 would collide with a real cluster from discovery.
USER_CLUSTER_ID = -1


def _conn():
    """An open connection this module owns and closes itself.

    Not db.get_connection() — that is a @contextmanager, so calling it returns
    the manager object rather than a connection. Every use here follows the
    open / try / close shape, so it needs the raw connection.
    """
    return db.get_raw_connection()


def _norm_values(values) -> Optional[list]:
    """Normalize one feature's values so two spellings of the same combination
    produce the same key: sorted, de-duplicated, stringified, empty → None."""
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    cleaned = sorted({str(v).strip() for v in values
                      if v is not None and str(v).strip()})
    return cleaned or None


def combo_key(combo: dict, entity: str | None = None) -> str:
    """The identity of a combination, independent of any clustering run.

    Certification matches on this. cluster_id is excluded deliberately: which
    cluster a discovery run assigned a combination to is an artifact of that
    run, not a property of the pattern. The record kind is included (a tag in
    the pack's tag level, or the record key in front: pattern_scope) so a
    primary and a child pattern over the same scope never certify each other.
    """
    entity = scope._known(entity)
    tag = scope.tag_of(entity)
    parts = []
    for feature in FEATURES:
        values = _norm_values(combo.get(feature))
        if feature == scope.TAG_COLUMN:
            values = sorted((values or []) + ([tag] if tag else [])) or None
        parts.append(str(values or "ALL"))
    return scope.key_prefix(entity) + "|".join(parts)


def entity_of(pattern: dict) -> str:
    """Which record kind a stored pattern describes (pattern_scope.entity_of):
    its scope says so, or, on a row from before it, the markers of its day."""
    return scope.entity_of(pattern)


# Kept as the internal spelling used throughout this module.
_entity_of = entity_of


def _row_to_pattern(row: dict) -> dict:
    """Shape a stored row for the API, with provenance made explicit."""
    origin = str(row.get("origin") or DISCOVERED)
    certified = row.get("certified_at") is not None
    return {
        "pattern_id": int(row["id"]),
        "name": str(row.get("contextual_name") or f"Pattern {row['id']}"),
        "origin": origin,
        "certified": certified,
        "certified_at": row["certified_at"].isoformat() if certified else None,
        "certified_by": row.get("certified_by"),
        "certified_against_id": row.get("certified_against_id"),
        "editable": origin == USER_DEFINED and row.get("archived_at") is None,
        # Found under an earlier build of a generated pack: kept read-only.
        "archived": row.get("archived_at") is not None,
        "plan_version": row.get("plan_version"),
        "member_count": int(row.get("member_count") or 0),
        "combo_key": row.get("combo_key"),
        "entity": _entity_of(row),
        "features": {f: _scope_values(row).get(f) for f in FEATURES},
        "labels": scope.named_labels(scope.labels_of(row)),
        "created_by": row.get("created_by"),
        "saved": bool(row.get("saved_at")),
    }


def _refuse_archived(row: dict) -> None:
    """A pattern found under an earlier build of the pack is read-only."""
    if row.get("archived_at") is not None:
        raise db.PatternArchived(
            f"“{row.get('contextual_name')}” was found under an earlier build of the pack "
            f"({str(row.get('plan_version') or '')[:12]}) and is kept read-only.")


def _scope_values(row: dict) -> dict:
    """A stored pattern's scope values, without the record-kind tag."""
    return scope.filters_of(row)


def _parse(value):
    """Read one stored feature back into a list of values, or None.

    A feature left unset is stored as the JSON literal `null`. Wrapping that in
    a list produced [None], which _norm_values then stringified to the literal
    ["None"] — a feature value that matches nothing, silently rewrites the
    pattern's combo_key on an unrelated rename, and collides with the next
    pattern edited the same way (a UNIQUE violation surfacing as a 500).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None] or None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [str(value)]
    if parsed is None:
        return None
    if isinstance(parsed, list):
        return [v for v in parsed if v is not None] or None
    return [parsed]


# ── Reads ────────────────────────────────────────────────────────────────────

_CATALOG_NS = "patterns.catalog"

# Bump when _row_to_pattern changes what it produces. The table signature keyed
# below tracks the *data*, not this code, so without a version marker a cached
# catalog would survive an edit to the transformation and keep serving the old
# shape. entity_of already moved once during this work; that is exactly the case
# this guards.
# v2: get_all_patterns gained an id tiebreaker, so lists cached under v1 carry
# the old, unstable ordering.
_CATALOG_VERSION = 2


def _catalog() -> list[dict]:
    """Every pattern, shaped for the API, from the temp-file cache.

    db.get_all_patterns already caches the rows; this caches the *transformed*
    list on top, because rebuilding 609 shaped dicts costs more than reading
    them back. Both are keyed on the same table signature, so a write to
    sdm.patterns invalidates this in the same breath as the rows underneath.

    Measured on 609 patterns: 36.5 ms to query and transform, 3.2 ms to check
    the signature and unpickle.
    """
    from . import disk_cache

    try:
        key = disk_cache.make_key(_CATALOG_NS, _CATALOG_VERSION,
                                  db.patterns_signature())
        hit = disk_cache.get(key)
        if hit is not None:
            return hit
    except Exception:
        # A cache that cannot answer must never stop the read.
        return [_row_to_pattern(r) for r in (db.get_all_patterns() or [])]

    started = time.perf_counter()
    out = [_row_to_pattern(r) for r in (db.get_all_patterns() or [])]
    try:
        disk_cache.put(key, out, ttl=1800, label=_CATALOG_NS,
                       namespace=_CATALOG_NS,
                       cost_seconds=time.perf_counter() - started)
    except Exception:
        pass
    return out


def list_patterns(origin: Optional[str] = None) -> list[dict]:
    # One cached list serves every origin filter; filtering here costs
    # microseconds and avoids an entry per filter value.
    out = _catalog()
    if origin:
        return [p for p in out if p["origin"] == origin]
    return out


def get(pattern_id: int) -> Optional[dict]:
    row = db.get_pattern(pattern_id)
    return _row_to_pattern(row) if row else None


# ── Creating a user-defined pattern ──────────────────────────────────────────

def create_user_pattern(name: str, features: dict, entity: str | None = None,
                        created_by: str = "user",
                        labels: Optional[dict] = None) -> dict:
    """Define a new pattern from a combination of feature values.

    Values are not checked against reference data. A new line of business has
    codes that exist nowhere yet, and rejecting them would make this feature
    useless for the case it was asked for. The pattern carries the user_defined
    flag instead, so anyone reading it knows the tool has not vouched for it.

    No distribution is stored. A pattern is a combination; how many records to
    produce, and in what mix, is decided at generation time.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("A pattern needs a name.")

    values = {f: _norm_values(features.get(f)) for f in FEATURES}
    if not any(values.values()):
        raise ValueError(
            "A pattern needs at least one feature value — otherwise it matches "
            "everything and describes nothing."
        )

    ck = combo_key(values, entity)

    existing = _find_by_combo(ck)
    mine = [p for p in existing if p["origin"] == USER_DEFINED]
    if mine:
        raise ValueError(
            f"“{mine[0]['name']}” already defines this exact combination. "
            f"Edit that one, or clone it if you want a variant."
        )

    entity = scope._known(entity)
    labels = scope.labels_from_names(labels)
    conn = _conn()
    try:
        cur = conn.cursor()
        from datetime import datetime

        from sdg.analyzer.db import insert_row

        scope.require_column(db._table_shape(cur, PATTERNS)[1])
        new_id = insert_row(cur, PATTERNS, {
            "pattern_key": f"{ck}|{USER_CLUSTER_ID}", "combo_key": ck,
            "contextual_name": clean_name,
            **scope.columns(entity, values, labels, tagged=_tagged(values, entity)),
            "cluster_id": USER_CLUSTER_ID, "member_count": 0, "origin": USER_DEFINED,
            "created_by": created_by, "saved_by": created_by, "saved_at": datetime.now(),
            **db.build_stamp(),
        })
        conn.commit()
    finally:
        conn.close()

    return {"pattern_id": new_id, "name": clean_name, "origin": USER_DEFINED,
            "combo_key": ck, "matches": [m["pattern_id"] for m in existing]}


def _tagged(values: dict, entity: str) -> dict:
    """Values as the pack's own columns keep them: the record-kind tag folded
    into the tag level, sorted (as user-defined rows always were)."""
    tag = scope.tag_of(entity)
    if not tag:
        return values
    return {**values, scope.TAG_COLUMN: sorted((values.get(scope.TAG_COLUMN) or []) + [tag])}


def clone_pattern(source_id: int, name: str, features: Optional[dict] = None,
                  created_by: str = "user") -> dict:
    """Clone any pattern, optionally changing feature values.

    The result is always user_defined, even when cloned from a discovered
    pattern and even when no value was changed. A copy is not the evidence —
    only the discovered record itself is — and letting a clone inherit the
    discovered flag would make the flag meaningless.
    """
    source = db.get_pattern(source_id)
    if not source:
        raise ValueError(f"Pattern {source_id} not found.")
    # Its profile was measured on an earlier build's tables: no new pattern
    # is made from it.
    _refuse_archived(source)

    # Without the record-kind tag: bookkeeping, not a feature value, it must
    # not survive into the clone's stated values or it becomes an unusable
    # filter.
    base = {f: _scope_values(source).get(f) for f in FEATURES}
    entity = _entity_of(source)

    if features:
        for f in FEATURES:
            if f in features:
                base[f] = features[f]

    result = create_user_pattern(
        name=name or f"Copy of {source.get('contextual_name')}",
        features=base, entity=entity, created_by=created_by,
        labels=scope.labels_of(source),
    )
    result["cloned_from"] = source_id
    return result


def update_user_pattern(pattern_id: int, name: Optional[str] = None,
                        features: Optional[dict] = None) -> dict:
    """Edit a user-defined pattern. Discovered patterns are refused."""
    row = db.get_pattern(pattern_id)
    if not row:
        raise ValueError(f"Pattern {pattern_id} not found.")
    _refuse_archived(row)
    if str(row.get("origin") or DISCOVERED) == DISCOVERED:
        raise ValueError(
            f"“{row.get('contextual_name')}” was discovered from real data and "
            f"cannot be edited. Clone it to create an editable variant."
        )

    entity = _entity_of(row)
    values = {f: _scope_values(row).get(f) for f in FEATURES}
    if features:
        for f in FEATURES:
            if f in features:
                values[f] = features[f]
    values = {f: _norm_values(values.get(f)) for f in FEATURES}

    if not any(values.values()):
        raise ValueError("A pattern needs at least one feature value.")

    ck = combo_key(values, entity)
    stored = scope.columns(entity, values, scope.labels_of(row),
                           tagged=_tagged(values, entity))
    new_name = (name or row.get("contextual_name") or "").strip()

    # Changing the combination invalidates any certification: it was awarded to
    # a different combination, and carrying it over would assert evidence that
    # was never gathered for what the pattern now says.
    combo_changed = ck != str(row.get("combo_key") or "")

    if combo_changed:
        # The same check create_user_pattern makes: another user-defined
        # pattern already holding this combination would otherwise surface as
        # the store's unique-key violation (a server error) on the UPDATE.
        mine = [p for p in _find_by_combo(ck)
                if p["origin"] == USER_DEFINED and p["pattern_id"] != pattern_id]
        if mine:
            raise ValueError(
                f"“{mine[0]['name']}” already defines this exact combination. "
                f"Edit that one, or change a value so the two differ."
            )

    conn = _conn()
    try:
        cur = conn.cursor()
        if combo_changed:
            # The scope's columns: the pack's own, and scope_json where the
            # store has it.
            # Where the shape cannot be read, the pack's own columns only
            # (their names are plain: pattern_scope checks them).
            have = db._table_shape(cur, PATTERNS)[1]
            scope_set = {c: v for c, v in stored.items()
                         if c != "entity" and (c in have if have else c != "scope_json")}
            sets = ", ".join(f"[{c}] = ?" for c in scope_set)
            cur.execute(f"""
                UPDATE {PATTERNS} SET contextual_name = ?, combo_key = ?,
                    pattern_key = ?, {sets + "," if sets else ""} certified_at = NULL,
                    certified_by = NULL, certified_against_id = NULL,
                    updated_at = GETDATE()
                WHERE id = ?
            """, new_name, ck, f"{ck}|{USER_CLUSTER_ID}", *scope_set.values(), pattern_id)
        else:
            cur.execute(
                f"UPDATE {PATTERNS} SET contextual_name = ?, updated_at = GETDATE() "
                f"WHERE id = ?", new_name, pattern_id)
        conn.commit()
    finally:
        conn.close()

    return {"pattern_id": pattern_id, "name": new_name, "combo_key": ck,
            "certification_cleared": combo_changed and row.get("certified_at") is not None}


# ── Certification ────────────────────────────────────────────────────────────

def _find_by_combo(ck: str) -> list[dict]:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {PATTERNS} WHERE combo_key = ?", ck)
        cols = [d[0] for d in cur.description]
        # A pattern of an earlier build is kept, read-only, but is no longer
        # one of this combination's patterns.
        return [p for p in (_row_to_pattern(dict(zip(cols, r))) for r in cur.fetchall())
                if not p["archived"]]
    finally:
        conn.close()


def review_queue() -> list[dict]:
    """User-defined patterns whose combination discovery has since found.

    Computed live rather than stored. A queue table would go stale the moment a
    discovery run changed what exists, and would need its own reconciliation;
    the comparison is a single indexed lookup per candidate.
    """
    everything = list_patterns()
    discovered = {}
    for p in everything:
        if p["origin"] == DISCOVERED and p["combo_key"]:
            discovered.setdefault(p["combo_key"], []).append(p)

    queue = []
    for p in everything:
        if p["origin"] != USER_DEFINED or p["certified"]:
            continue
        matches = discovered.get(p["combo_key"] or "", [])
        if not matches:
            continue
        best = max(matches, key=lambda m: m["member_count"])
        queue.append({
            "user_pattern": p,
            "matched_pattern": best,
            "other_matches": len(matches) - 1,
            "observed_members": best["member_count"],
        })

    queue.sort(key=lambda q: q["observed_members"], reverse=True)
    return queue


def certify(user_pattern_id: int, discovered_id: int,
            approved_by: str = "user") -> dict:
    """Merge a user-defined pattern with the discovered pattern that matches it.

    The user-defined record is kept and marked certified rather than replaced.
    Layouts, saved configurations and past generation runs reference it by id;
    replacing it would break every one of those references, and deleting the
    discovered pattern would destroy evidence.

    Never called automatically. A reviewer approves each merge, because data
    generated last month under a hypothesis must not retroactively appear to
    have been evidence-backed.
    """
    user = db.get_pattern(user_pattern_id)
    if not user:
        raise ValueError(f"Pattern {user_pattern_id} not found.")
    if str(user.get("origin") or DISCOVERED) != USER_DEFINED:
        raise ValueError("Only a user-defined pattern can be certified.")
    if user.get("certified_at") is not None:
        raise ValueError(f"“{user.get('contextual_name')}” is already certified.")

    found = db.get_pattern(discovered_id)
    if not found:
        raise ValueError(f"Pattern {discovered_id} not found.")
    _refuse_archived(user)
    _refuse_archived(found)
    if str(found.get("origin") or DISCOVERED) != DISCOVERED:
        raise ValueError("A pattern can only be certified against a discovered one.")

    if str(user.get("combo_key") or "") != str(found.get("combo_key") or ""):
        raise ValueError(
            "These two do not describe the same combination, so one cannot "
            "certify the other."
        )

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE {PATTERNS}
            SET certified_at = ?, certified_by = ?, certified_against_id = ?,
                member_count = ?, profile_json = COALESCE(?, profile_json),
                updated_at = GETDATE()
            WHERE id = ?
        """, datetime.now(), approved_by, discovered_id,
            int(found.get("member_count") or 0), found.get("profile_json"),
            user_pattern_id)
        conn.commit()
    finally:
        conn.close()

    return {
        "pattern_id": user_pattern_id,
        "certified_against_id": discovered_id,
        "certified_by": approved_by,
        "observed_members": int(found.get("member_count") or 0),
        "name": user.get("contextual_name"),
    }
