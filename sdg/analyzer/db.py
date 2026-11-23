"""SQL Server backend for pattern storage, rules, tags, and keyword vocabulary.

SQL Server only - no fallback. All tables created by SDM_SQL_Server_Setup.sql.
"""

import json

from contextlib import contextmanager

import pyodbc

from sdg.packs import held

from .config import get_connection_string, SQL_SCHEMA

_SCHEMA = SQL_SCHEMA


# ── Inserting into tables that may not be this tool's ─────────────────────────
#
# On a customer's side the store's tables are theirs: an `id` may be a plain
# int rather than an IDENTITY, and `created_at` may have no default. Every
# insert into such a table goes through insert_row, which assigns the id when
# the database will not and stamps the times when nothing else does. What a
# table is like is read once per process.

_TABLE_SHAPE: dict[str, tuple[bool, set[str]]] = {}


def _table_shape(cur, table: str) -> tuple[bool, set[str]]:
    """Whether `table`.id is an identity column, and the table's columns."""
    if table not in _TABLE_SHAPE:
        try:
            cur.execute(
                "SELECT c.name, c.is_identity FROM sys.columns c "
                "WHERE c.object_id = OBJECT_ID(?)", (table,))
            rows = cur.fetchall()
        except Exception:
            # Nothing to read the table's shape from: insert as the database
            # would want by default, and let it assign the id.
            return True, set()
        columns = {str(r[0]) for r in rows}
        identity = any(str(r[0]).lower() == "id" and bool(r[1]) for r in rows)
        if table.lower().endswith(".patterns") and not {"scope_json", "archived_at"} <= columns:
            # Read again next time: the column is added while the tool runs
            # (01_sdm_upgrade.sql), and kept from then on.
            return identity, columns
        _TABLE_SHAPE[table] = (identity, columns)
    return _TABLE_SHAPE[table]


def insert_row(cur, table: str, values: dict) -> int:
    """Insert one row into a store table and return its id.

    Where `id` is an identity the database assigns it. Where it is a plain int
    the next one is taken under an update lock held to the end of the
    transaction, so two saves at once cannot take the same id.
    """
    identity, columns = _table_shape(cur, table)
    row = {k: v for k, v in values.items() if not columns or k in columns}
    stamped = [c for c in ("created_at", "updated_at") if c in columns and c not in row]
    names = list(row) + stamped
    marks = ["?"] * len(row) + ["SYSDATETIME()"] * len(stamped)
    params = list(row.values())
    if identity or "id" not in columns:
        cur.execute(
            f"INSERT INTO {table} ({', '.join(names)}) OUTPUT INSERTED.id "
            f"VALUES ({', '.join(marks)})", *params)
        got = cur.fetchone()
        return int(got[0]) if got else 0
    cur.execute(f"SELECT ISNULL(MAX(id), 0) + 1 FROM {table} WITH (UPDLOCK, HOLDLOCK)")
    new_id = int(cur.fetchone()[0])
    cur.execute(
        f"INSERT INTO {table} (id, {', '.join(names)}) VALUES (?, {', '.join(marks)})",
        new_id, *params)
    return new_id


@contextmanager
def get_connection():
    """Yield a pyodbc connection to SQL Server."""
    try:
        conn = pyodbc.connect(get_connection_string(), autocommit=False)
    except pyodbc.Error as e:
        raise ConnectionError(
            f"Cannot connect to SQL Server. Check SDG_DB_DENORM and SDG_DENORM_* in .env. Error: {e}"
        ) from e
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dict_rows(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# -- Pattern CRUD -------------------------------------------------------------

# The scope levels a combination's key is made of, in order. Stored keys are
# made of them, so their order is fixed once patterns are kept.
COMBO_KEY = tuple(held("features:store.combo_key"))


def _make_combo_key(combo):
    """Identity of a combination, independent of any clustering run.

    This is pattern_key without the trailing cluster id, which is exactly what
    SDM_Migration_Pattern_Provenance.sql back-fills into combo_key. Certification
    matches on it, so a pattern stored without one can never be certified — and
    discovery used to store every pattern that way, leaving the migration as the
    only thing that ever populated the column.

    The three keys are derived from one place so they cannot drift apart.
    """
    from sdg.analyzer.pattern_scope import key_prefix

    prefix = key_prefix(combo["_entity"]) if "_entity" in combo else ""
    return prefix + "|".join(str(combo.get(level) or "ALL") for level in COMBO_KEY)


def _make_pattern_key(combo, cluster_id):
    return f"{_make_combo_key(combo)}|{cluster_id}"


def upsert_pattern(combo, cluster_id, contextual_name, member_count,
                    pct_of_pop, silhouette, profile=None, ai_summary=None,
                    conn=None, pattern_key=None):
    """Insert or update one pattern.

    Pass `conn` to reuse an open connection. A discovery walk persists several
    patterns per combination — hundreds per run — and opening a fresh
    connection for each one cost more than the upsert itself.
    """
    if conn is not None:
        return _upsert_pattern_on(conn, combo, cluster_id, contextual_name,
                                  member_count, pct_of_pop, silhouette,
                                  profile, ai_summary, pattern_key)
    with get_connection() as owned:
        return _upsert_pattern_on(owned, combo, cluster_id, contextual_name,
                                  member_count, pct_of_pop, silhouette,
                                  profile, ai_summary, pattern_key)


def _upsert_pattern_on(conn, combo, cluster_id, contextual_name, member_count,
                       pct_of_pop, silhouette, profile=None, ai_summary=None,
                       pattern_key=None):
    # A saved pattern brings its own key. The hierarchy-and-cluster key names
    # a slot in a discovery walk, and a pattern saved from an analysis with a
    # chosen k, or from chosen records, is not that slot -- sharing the key
    # would overwrite one with the other.
    from sdg.analyzer import pattern_scope

    key = pattern_key or _make_pattern_key(combo, cluster_id)
    combo_key = _make_combo_key(combo)
    profile_json = json.dumps(profile, default=str) if profile else None
    # The scope, in the pack's words (scope_json), and in the pack's own
    # columns where it has them -- those as the key builder made them.
    scope = pattern_scope.columns(combo.get("_entity"), combo, combo.get("_labels"),
                                  tagged=combo)
    cur = conn.cursor()
    shape = _table_shape(cur, f"{_SCHEMA}.patterns")[1]
    pattern_scope.require_column(shape)
    cur.execute(f"SELECT id FROM {_SCHEMA}.patterns WHERE pattern_key = ?", (key,))
    existing = cur.fetchone()
    if existing:
        cur.execute(f"""
            UPDATE {_SCHEMA}.patterns SET
                contextual_name = ?, member_count = ?, pct_of_pop = ?,
                silhouette = ?, profile_json = ?, combo_key = ?,
                ai_summary = COALESCE(?, ai_summary), updated_at = GETDATE()
            WHERE pattern_key = ?
        """, (contextual_name, member_count, pct_of_pop, silhouette,
              profile_json, combo_key, ai_summary, key))
        stamp = build_stamp()
        if stamp and {"pack_id", "plan_version"} <= shape:
            # A row written before builds were stamped takes the running one's.
            cur.execute(f"UPDATE {_SCHEMA}.patterns SET pack_id = ?, plan_version = ? "
                        f"WHERE pattern_key = ? AND pack_id IS NULL",
                        (stamp["pack_id"], stamp["plan_version"], key))
        if "scope_json" in shape:
            # A row from before scope_json gets one; one kept without names
            # (a walk that had none) gets them once they are known. A scope
            # already named is the row's and is left as it is.
            cur.execute(f"UPDATE {_SCHEMA}.patterns SET scope_json = ?, entity = ? "
                        f"WHERE pattern_key = ? AND (scope_json IS NULL OR "
                        f"(scope_json LIKE ? AND ? NOT LIKE ?))",
                        (scope["scope_json"], scope["entity"], key,
                         pattern_scope.UNNAMED, scope["scope_json"], pattern_scope.UNNAMED))
        return existing[0]
    else:
        return insert_row(cur, f"{_SCHEMA}.patterns", {
            "pattern_key": key, "combo_key": combo_key,
            "contextual_name": contextual_name,
            **scope,
            **build_stamp(),
            "cluster_id": cluster_id, "member_count": member_count,
            "pct_of_pop": pct_of_pop, "silhouette": silhouette,
            "profile_json": profile_json, "ai_summary": ai_summary,
        })


def get_pattern(pattern_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        if cur.description is None:
            return None
        row = cur.fetchone()
        if row is None:
            return None
        columns = [col[0] for col in cur.description]
        return dict(zip(columns, row))


# ── Pattern reads ────────────────────────────────────────────────────────────
#
# The signature is a 1.9 ms aggregate that moves on every write path in the
# codebase: COUNT(*) catches inserts and deletes, COUNT(saved_at) catches save
# and unsave (which touch neither updated_at nor the row count), MAX(updated_at)
# catches edits, and MAX(id) catches an insert that replaces a delete. Any write
# therefore produces a different key.


def patterns_signature():
    """Cheap fingerprint of the patterns table — changes on any write."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*), COUNT(saved_at), MAX(updated_at), MAX(id)
            FROM {_SCHEMA}.patterns
        """)
        return tuple(str(v) for v in cur.fetchone())


def get_all_patterns(include_archived: bool = True):
    """Every pattern; without `include_archived`, only those of the current
    build (an archived one is kept to read, not to match or generate from)."""
    with get_connection() as conn:
        cur = conn.cursor()
        if not include_archived and "archived_at" in _table_shape(cur, f"{_SCHEMA}.patterns")[1]:
            cur.execute(f"SELECT * FROM {_SCHEMA}.patterns WHERE archived_at IS NULL "
                        f"ORDER BY member_count DESC, id")
            return _dict_rows(cur)
        # id breaks ties. 172 member_count values are shared by more than one
        # pattern here (the largest group is 8), and without a tiebreaker SQL
        # Server is free to order them differently between queries — which a
        # paginated caller sees as rows repeated on one page and missing from
        # the next.
        cur.execute(
            f"SELECT * FROM {_SCHEMA}.patterns ORDER BY member_count DESC, id")
        return _dict_rows(cur)


# -- Saved Patterns -----------------------------------------------------------

def pattern_id_for_key(pattern_key):
    """The id of the pattern stored under this key, or None."""
    with get_connection() as conn:
        row = conn.cursor().execute(
            f"SELECT id FROM {_SCHEMA}.patterns WHERE pattern_key = ?", (pattern_key,)
        ).fetchone()
    return int(row[0]) if row else None


class PatternArchived(Exception):
    """A write to a pattern found under an earlier build of a generated pack:
    kept read-only (the API answers 409). Not a ValueError: nothing the
    person typed is wrong."""


def build_stamp() -> dict:
    """What a pattern written now records of the build it was found under:
    the pack's id and build hash, where a build wrote the running pack."""
    from sdg import packs

    pack = packs.active()
    if not pack.built_hash:
        return {}
    return {"pack_id": pack.id, "plan_version": pack.built_hash}


def refuse_archived(cur, pattern_id) -> None:
    """Refuse a write to an archived pattern. Nothing on a store without the
    archive's columns (that store can archive nothing); any other failure of
    the check is raised, never taken as "not archived"."""
    try:
        row = cur.execute(f"SELECT archived_at, plan_version, contextual_name FROM "
                          f"{_SCHEMA}.patterns WHERE id = ?", (pattern_id,)).fetchone()
    except pyodbc.ProgrammingError as exc:
        if "(207)" in str(exc):  # invalid column name: no archive's columns
            return
        raise
    if row is not None and row[0] is not None:
        raise PatternArchived(
            f"“{row[2]}” was found under an earlier build of the pack "
            f"({str(row[1] or '')[:12]}) and is kept read-only.")


def archive_other_builds(pack_id: str, version: str, *, every: bool = False) -> int | None:
    """Archive the pack's patterns found under any build but `version` (with
    `every`, all its current patterns: the build's tables were just loaded
    for the first time, so a pattern made before describes the earlier rows):
    kept read-only, each key freed for the new build's walk (the key gains
    '#' and the row's id). How many; None when the store lacks the archive's
    columns (the table setup adds them)."""
    with get_connection() as conn:
        cur = conn.cursor()
        if not {"pack_id", "plan_version", "archived_at"} <= \
                _table_shape(cur, f"{_SCHEMA}.patterns")[1]:
            return None
        cur.execute(f"""
            UPDATE {_SCHEMA}.patterns
            SET archived_at = SYSDATETIME(), updated_at = GETDATE(),
                pattern_key = LEFT(pattern_key, 188) + N'#' + CAST(id AS NVARCHAR(11))
            WHERE pack_id = ? AND archived_at IS NULL
              AND (? = 1 OR plan_version IS NULL OR plan_version <> ?)
        """, (pack_id, 1 if every else 0, version))
        return int(cur.rowcount or 0)


def mark_pattern_saved(pattern_id, saved_by="user"):
    """Mark a pattern as saved/favorited."""
    with get_connection() as conn:
        cur = conn.cursor()
        refuse_archived(cur, pattern_id)
        cur.execute(f"""
            UPDATE {_SCHEMA}.patterns
            SET saved_by = ?, saved_at = GETDATE()
            WHERE id = ?
        """, (saved_by, pattern_id))


def mark_pattern_origin(pattern_id, origin, created_by="user"):
    """Record where a pattern came from: found by analysis, or defined."""
    with get_connection() as conn:
        cur = conn.cursor()
        refuse_archived(cur, pattern_id)
        cur.execute(f"""
            UPDATE {_SCHEMA}.patterns
            SET origin = ?, created_by = ?
            WHERE id = ?
        """, (origin, created_by, pattern_id))


def mark_pattern_unsaved(pattern_id):
    """Remove saved status from a pattern."""
    with get_connection() as conn:
        cur = conn.cursor()
        refuse_archived(cur, pattern_id)
        cur.execute(f"""
            UPDATE {_SCHEMA}.patterns
            SET saved_by = NULL, saved_at = NULL
            WHERE id = ?
        """, (pattern_id,))


def get_saved_patterns():
    """Fetch all saved patterns ordered by saved_at desc."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM {_SCHEMA}.patterns
            WHERE saved_by IS NOT NULL
            ORDER BY saved_at DESC
        """)
        return _dict_rows(cur)


def is_pattern_saved(pattern_id):
    """Check if a specific pattern is saved."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT saved_by FROM {_SCHEMA}.patterns WHERE id = ? AND saved_by IS NOT NULL", (pattern_id,))
        return cur.fetchone() is not None


def save_pattern_from_profiler(combo, cluster_id, contextual_name, member_count,
                                pct_of_pop, silhouette, profile=None,
                                ai_summary=None, saved_by="user"):
    """Upsert a pattern and mark it as saved. Returns pattern ID.

    Handles dedup via pattern_key — if the exact hierarchy + cluster combo
    already exists, it updates and marks saved. Otherwise inserts new.
    """
    pattern_id = upsert_pattern(
        combo, cluster_id, contextual_name, member_count,
        pct_of_pop, silhouette, profile, ai_summary,
    )
    if pattern_id:
        mark_pattern_saved(pattern_id, saved_by)
    return pattern_id


# -- Pattern Tags -------------------------------------------------------------

def get_tags_batch(pattern_ids):
    """Fetch tags for multiple patterns in one query. Returns dict of pattern_id → [tags]."""
    if not pattern_ids:
        return {}
    safe_ids = ",".join(str(int(x)) for x in pattern_ids)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT pattern_id, tag, source, confirmed_by
            FROM {_SCHEMA}.pattern_tags
            WHERE pattern_id IN ({safe_ids})
            ORDER BY pattern_id, tag
        """)
        rows = _dict_rows(cur)

    result = {}
    for row in rows:
        pid = row["pattern_id"]
        if pid not in result:
            result[pid] = []
        result[pid].append(row)
    return result


# -- Tag Vocabulary -----------------------------------------------------------

def resolve_synonyms(terms):
    if not terms:
        return []
    clean = [t.lower().strip() for t in terms]
    resolved = []
    with get_connection() as conn:
        cur = conn.cursor()
        for term in clean:
            cur.execute(f"SELECT canonical_tag FROM {_SCHEMA}.tag_vocabulary WHERE synonym = ?", (term,))
            row = cur.fetchone()
            resolved.append(row[0] if row else term)
    return list(set(resolved))


# -- Generation Rules ---------------------------------------------------------

def delete_generation_rule(field_name):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_SCHEMA}.generation_rules WHERE field_name = ?", (field_name,))


def get_raw_connection():
    """An open connection the caller owns and must close.

    get_connection is a context manager that commits and closes on exit, which
    is right for a single operation and wrong for a long walk that wants one
    connection across hundreds of writes.
    """
    try:
        return pyodbc.connect(get_connection_string(), autocommit=False)
    except pyodbc.Error as e:
        raise ConnectionError(
            f"Cannot connect to SQL Server. Check SDG_DB_DENORM and SDG_DENORM_* in .env. Error: {e}"
        ) from e
