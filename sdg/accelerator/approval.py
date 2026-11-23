"""Approval of a source plan, kept in the store's sdm.source_plans.

Modeled on the layouts' approval (layout_service.approve_layout): a plan is
submitted for review, approved by a conditional UPDATE that holds only for
the plan as it was submitted, and withdrawn back to draft. One row per
source holds the plan's snapshot and its hash (plan.plan_hash) as submitted.

- `submit` refuses a plan with problems (`problems`) and upserts the row as
  pending_review, clearing any earlier approval.
- `approve` holds only while the row is pending_review and its hash is the
  one given -- the hash of the plan the approver looked at. With a second
  approver set (the layouts' setting, read by their own reader), it is
  recorded in that approver's name, typed to confirm (`approver`), and the
  approver may not be the one who submitted.
- `changed` drops the row to draft when the plan saved is no longer the one
  submitted: every edit calls it, so any edit drops an approval.
- `approved_plan` is what a build reads: refused unless the row is approved
  and the plan saved now has the hash that was approved.
- `record_build` records a build's hash, and the hash of the plan it was
  built from (built_plan_hash), in the transaction that finds the plan
  still approved and unchanged, putting the built folder in place inside
  it: a plan withdrawn or edited meanwhile is never recorded built, and a
  build is shown only while the plan approved is the plan it was built from.

Who submits and who confirms is the computer's user, as for layouts; SDM has
no sign-in. Everything here is written to the store (the denorm connection,
never a source, never a protected database: get_connection_string refuses
one), with parameters only. Nothing here reads a value of any source column.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sdg.accelerator import plan as planning
from sdg.accelerator import sources
from sdg.accelerator.plan import SourcePlan
from sdg.accelerator.structure import PRIVACY
from sdg.accelerator.tables import ROLES

TABLE_NAME = "source_plans"
STATUSES = ("draft", "pending_review", "approved")

# Written for schema sdm; the configured schema replaces it (`_sql`). The
# same statement is in scripts/customer/setup_tables.sql and seed/ddl_sdm.sql
# (tests/test_accelerator_approval.py keeps the three equal). Add-only.
DDL = """IF OBJECT_ID(N'sdm.source_plans', N'U') IS NULL
CREATE TABLE sdm.source_plans (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    source        NVARCHAR(32)  NOT NULL,
    pack_id       NVARCHAR(32)  NULL,
    status        NVARCHAR(20)  NOT NULL DEFAULT 'draft',
    plan_json     NVARCHAR(MAX) NOT NULL,
    plan_hash     CHAR(64)      NOT NULL,
    submitted_by  NVARCHAR(100) NULL,
    submitted_at  DATETIME2     NULL,
    approved_by   NVARCHAR(100) NULL,
    approved_at   DATETIME2     NULL,
    confirmed_by  NVARCHAR(100) NULL,
    built_hash    CHAR(64)      NULL,
    built_at      DATETIME2     NULL,
    built_plan_hash CHAR(64)    NULL,
    CONSTRAINT UQ_source_plans_source UNIQUE (source),
    CONSTRAINT CK_source_plans_status
        CHECK (status IN ('draft', 'pending_review', 'approved'))
);"""

# A table made before built_plan_hash was added gains it, NULL. Add-only, and
# in the setup scripts too, as its own batch after the CREATE TABLE.
DDL_ADDED = """IF COL_LENGTH(N'sdm.source_plans', N'built_plan_hash') IS NULL
    ALTER TABLE sdm.source_plans ADD built_plan_hash CHAR(64) NULL;"""

# The build whose tables were last loaded into the store (phase 4): its first
# load archives the pack's earlier patterns. Add-only, as its own batch.
DDL_LOADED = """IF COL_LENGTH(N'sdm.source_plans', N'loaded_hash') IS NULL
    ALTER TABLE sdm.source_plans ADD loaded_hash CHAR(64) NULL;"""

EXISTS = "SELECT OBJECT_ID(?, N'U')"

# The row, read alone or, inside a write, held until it commits.
SELECT = """SELECT status, plan_hash, submitted_by, submitted_at, approved_by,
       approved_at, confirmed_by, pack_id, built_hash, built_at, built_plan_hash
FROM sdm.source_plans WHERE source = ?"""
SELECT_HELD = """SELECT status, plan_hash, submitted_by, submitted_at, approved_by,
       approved_at, confirmed_by, pack_id, built_hash, built_at, built_plan_hash
FROM sdm.source_plans WITH (UPDLOCK, HOLDLOCK) WHERE source = ?"""
HAS_BUILT_PLAN_HASH = "SELECT COL_LENGTH(?, N'built_plan_hash')"
SELECT_BEFORE_BUILT_PLAN = """SELECT status, plan_hash, submitted_by, submitted_at, approved_by,
       approved_at, confirmed_by, pack_id, built_hash, built_at
FROM sdm.source_plans WHERE source = ?"""
COLUMNS = ("status", "plan_hash", "submitted_by", "submitted_at", "approved_by",
           "approved_at", "confirmed_by", "pack_id", "built_hash", "built_at",
           "built_plan_hash")

RESUBMIT = """UPDATE sdm.source_plans
SET status = 'pending_review', pack_id = ?, plan_json = ?, plan_hash = ?,
    submitted_by = ?, submitted_at = ?,
    approved_by = NULL, approved_at = NULL, confirmed_by = NULL
WHERE source = ?"""
INSERT = """INSERT INTO sdm.source_plans
    (source, pack_id, status, plan_json, plan_hash, submitted_by, submitted_at)
VALUES (?, ?, 'pending_review', ?, ?, ?, ?)"""

APPROVE = """UPDATE sdm.source_plans
SET status = 'approved', approved_by = ?, approved_at = ?, confirmed_by = ?
WHERE source = ? AND status = 'pending_review' AND plan_hash = ?"""

WITHDRAW = """UPDATE sdm.source_plans
SET status = 'draft', approved_by = NULL, approved_at = NULL, confirmed_by = NULL
WHERE source = ? AND status IN ('pending_review', 'approved')"""

# An edit: the row goes back to draft when the plan is no longer the one
# submitted. An edit that leaves the plan as it was changes nothing.
CHANGED = """UPDATE sdm.source_plans
SET status = 'draft', approved_by = NULL, approved_at = NULL, confirmed_by = NULL
WHERE source = ? AND status <> 'draft' AND plan_hash <> ?"""

# A build: recorded only while the plan is approved, and as approved, with
# the hash of the plan it was built from.
BUILT = """UPDATE sdm.source_plans
SET built_hash = ?, built_at = ?, built_plan_hash = plan_hash
WHERE source = ? AND status = 'approved' AND plan_hash = ?"""

CHANGED_SINCE = "changed since it was submitted"


class ApprovalRefused(ValueError):
    """A submit, approval or withdrawal the plan's state refuses, said in
    words the command line and the screen show as they are."""


class ApproverRefused(PermissionError):
    """The approver is not the one the setting names, or the setting cannot
    be read: nothing was approved."""


# --- the store -------------------------------------------------------------

def _schema() -> str:
    from sdg.config import get_settings

    # Checked as a plain SQL name when the settings are read (config._plain_schema).
    return get_settings().denorm_schema


def table() -> str:
    return f"{_schema()}.{TABLE_NAME}"


def _sql(statement: str) -> str:
    """A statement above in the configured schema."""
    return statement.replace("sdm.source_plans", table())


def _connect() -> Any:
    """The store's connection: get_connection_string refuses a protected
    database before connecting, as for every table of the store."""
    import pyodbc

    from sdg.analyzer.config import get_connection_string

    return pyodbc.connect(get_connection_string(), autocommit=False)


def ensure_table() -> None:
    """sdm.source_plans, created when the store has none (a store set up
    before it was added). Add-only."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(_sql(DDL))
        cursor.execute(_sql(DDL_ADDED))
        conn.commit()
    finally:
        conn.close()


def _row(cursor: Any, source: str, *, held: bool = False) -> dict[str, Any] | None:
    cursor.execute(_sql(SELECT_HELD if held else SELECT), source)
    found = cursor.fetchone()
    return dict(zip(COLUMNS, found)) if found else None


def record(source: str) -> dict[str, Any] | None:
    """The source's row as the store holds it (without the snapshot), or
    None when its plan was never submitted."""
    source = sources.plain_name(source)
    conn = _connect()
    try:
        cursor = conn.cursor()
        # A read creates nothing: a store without the table has nothing submitted.
        cursor.execute(EXISTS, table())
        if cursor.fetchone()[0] is None:
            return None
        # A table made before built_plan_hash was added: read without it (no
        # build is shown) until the next write adds it. A read changes nothing.
        cursor.execute(HAS_BUILT_PLAN_HASH, table())
        if cursor.fetchone()[0] is None:
            cursor.execute(_sql(SELECT_BEFORE_BUILT_PLAN), source)
            found = cursor.fetchone()
            return dict(zip(COLUMNS, (*found, None))) if found else None
        return _row(cursor, source)
    finally:
        conn.close()


# --- who approves ------------------------------------------------------------

def who() -> str:
    """The computer's user: who submits, and who confirms an approval."""
    from sdg.api.routers.layouts import _who

    return _who()


def second_approver() -> tuple[str | None, bool]:
    """The second approver the layouts' setting names (None when nobody is),
    and whether the setting is there but cannot be read: their own reader,
    so one setting governs both."""
    from sdg.api.routers.layouts import _setting

    return _setting()


def approver(typed: str | None) -> tuple[str, str]:
    """Who an approval is recorded for, and who confirmed it, as for layouts.

    With a second approver set, their name must be typed (any case, spaces
    at either end ignored): the approval is in that name, confirmed by the
    computer's user. With none set, both are the computer's user. Refused
    when the setting cannot be read: a damaged file never switches the
    control off."""
    from sdg.api.routers.layouts import same_name

    named, unreadable = second_approver()
    if unreadable:
        raise ApproverRefused("The approval setting can't be read. Set the second approver "
                              "again in Settings, Layout approval.")
    confirming = who()
    if named is None:
        return confirming, confirming
    if not same_name(typed, named):
        raise ApproverRefused("Type the second approver's name to approve this plan.")
    return named, confirming


# --- the gates before submit -------------------------------------------------

def _reached(level: Any, kind: dict[str, Any]) -> bool:
    """A level is reached from its record: on the record (or its lines)
    itself, or by a path of joins that ends at the level's table."""
    if level.table in (kind.get("table"), kind.get("lines_table")):
        return True
    return bool(level.path) and level.path[-1].to_table == level.table


def generated_config_problems(held: SourcePlan, graph: Any = None) -> list[str]:
    """Every column a generated configuration key names exists in the
    structure: a dry run of the builder's renderers (builder.config_problems).
    Given the key graph, the privacy classes are checked against the model
    and the store tables and extract are written too, as a build would."""
    from sdg.accelerator import builder

    return builder.config_problems(held, graph)


def problems(held: SourcePlan, graph: Any = None) -> list[str]:
    """Why the plan cannot be submitted yet; empty when it can. The command
    line and the screen pass the key graph, so the plan's SQL is written as a
    check before it is submitted; the build checks again either way."""
    found: list[str] = []
    for role in ROLES:
        kind = held.record_kinds.get(role)
        if not kind:
            found.append(f"Choose the {role} record kind.")
            continue
        name = kind.get("name") or {}
        if not (name.get("one") or "").strip() or not (name.get("many") or "").strip():
            found.append(f"Name the {role} record kind, one and many.")
        walks = [lv for lv in held.levels(role) if lv.selected and lv.kind == "walk"]
        if not any(_reached(lv, kind) for lv in walks):
            found.append(f"Select at least one walk level for the {role} record kind that "
                         f"its record reaches.")
        if not any(t.role == role for t in held.structure):
            found.append(f"The {role} record kind has no denorm table yet: edit the plan once "
                         f"so its structure is proposed.")
    for denorm in held.structure:
        for column in denorm.columns:
            if column.privacy not in PRIVACY:
                found.append(f"Give {denorm.name}.{column.name} a privacy class: "
                             f"{', '.join(PRIVACY)}.")
    found += generated_config_problems(held, graph)
    if not (held.pack or {}).get("id"):
        found.append("Name the pack: its id and the name a person reads.")
    return found


# --- submit, approve, withdraw -------------------------------------------------

def _transaction(work: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """Run `work(cursor)` in one transaction on the store: committed when it
    returns, rolled back when it raises."""
    ensure_table()
    conn = _connect()
    try:
        try:
            done = work(conn.cursor())
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return done
    finally:
        conn.close()


def submit(source: str, held: SourcePlan, by: str, graph: Any = None) -> dict[str, Any]:
    """Submit the plan for review: one row per source, pending_review, with
    the plan's snapshot and hash. Refused while the plan has problems (with
    the key graph, its SQL written as a check too), or when this very plan
    is approved already. Any earlier approval is cleared."""
    source = sources.plain_name(source)
    if held.source.upper() != source:
        raise ApprovalRefused(f"The plan is {held.source}'s, not {source}'s.")
    found = problems(held, graph)
    if found:
        raise ApprovalRefused("The plan cannot be submitted yet: " + " ".join(found))
    digest = planning.plan_hash(held)
    snapshot = held.to_dict()
    snapshot["status"] = "pending_review"
    body = json.dumps(snapshot, sort_keys=True)
    pack_id = (held.pack or {}).get("id")
    now = datetime.now()

    def work(cursor: Any) -> dict[str, Any]:
        row = _row(cursor, source, held=True)
        if row is None:
            cursor.execute(_sql(INSERT), source, pack_id, body, digest, by, now)
        else:
            if row["status"] == "approved" and row["plan_hash"] == digest:
                raise ApprovalRefused(f"{source}'s plan is approved already, as it stands.")
            # Submitted as it stands already: kept in its submitter's name.
            if row["status"] == "pending_review" and row["plan_hash"] == digest:
                raise ApprovalRefused(f"{source}'s plan is waiting for review already, as it "
                                      f"stands.")
            cursor.execute(_sql(RESUBMIT), pack_id, body, digest, by, now, source)
        return {"source": source, "status": "pending_review", "plan_hash": digest,
                "submitted_by": by, "pack_id": pack_id}

    return _transaction(work)


def approve(source: str, plan_hash: str, approved_by: str,
            confirmed_by: str | None = None) -> dict[str, Any]:
    """Approve the plan waiting for review, and only as it was submitted:
    `plan_hash` is the hash of the plan the approver looked at. With a
    second approver set, the one who submitted may not approve.
    `approved_by` and `confirmed_by` come from `approver`."""
    from sdg.api.routers.layouts import same_name

    source = sources.plain_name(source)
    named, unreadable = second_approver()
    if unreadable:
        raise ApproverRefused("The approval setting can't be read. Set the second approver "
                              "again in Settings, Layout approval.")
    # Checked here too, whoever calls: the approval is in the second
    # approver's name when one is set, and always confirmed by someone.
    if not (confirmed_by or "").strip() or not (approved_by or "").strip():
        raise ApproverRefused("An approval names who approved it and who confirmed it.")
    if named is not None and not same_name(approved_by, named):
        raise ApproverRefused("Type the second approver's name to approve this plan.")

    def work(cursor: Any) -> dict[str, Any]:
        row = _row(cursor, source, held=True)
        if row is None:
            raise ApprovalRefused(f"{source}'s plan was never submitted: submit it first.")
        if row["status"] != "pending_review":
            raise ApprovalRefused(f"{source}'s plan is {row['status'].replace('_', ' ')}: only a "
                                  f"plan waiting for review can be approved.")
        if row["plan_hash"] != plan_hash:
            raise ApprovalRefused(f"{source}'s plan has {CHANGED_SINCE}: submit it again.")
        if named is not None and same_name(approved_by, row["submitted_by"] or ""):
            raise ApprovalRefused(f"{approved_by} submitted {source}'s plan, so cannot approve "
                                  f"it: a second approver is set.")
        cursor.execute(_sql(APPROVE), approved_by, datetime.now(), confirmed_by, source,
                       plan_hash)
        if cursor.rowcount == 0:
            raise ApprovalRefused(f"{source}'s plan has {CHANGED_SINCE}: submit it again.")
        return {"source": source, "status": "approved", "plan_hash": plan_hash,
                "approved_by": approved_by, "confirmed_by": confirmed_by}

    return _transaction(work)


def withdraw(source: str) -> dict[str, Any]:
    """Take a submitted or approved plan back to draft, clearing its approval."""
    source = sources.plain_name(source)

    def work(cursor: Any) -> dict[str, Any]:
        cursor.execute(_sql(WITHDRAW), source)
        if cursor.rowcount == 0:
            row = _row(cursor, source)
            if row is None:
                raise ApprovalRefused(f"{source}'s plan was never submitted.")
            raise ApprovalRefused(f"{source}'s plan is a draft already.")
        return {"source": source, "status": "draft"}

    return _transaction(work)


def changed(source: str, new_hash: str) -> None:
    """The plan was saved again: its row goes back to draft when the plan is
    no longer the one submitted. Nothing is created: a store with no
    sdm.source_plans has nothing submitted."""
    source = sources.plain_name(source)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(EXISTS, table())
        if cursor.fetchone()[0] is None:
            return
        cursor.execute(_sql(CHANGED), source, new_hash)
        conn.commit()
    finally:
        conn.close()


def approved_plan(source: str) -> SourcePlan:
    """The plan saved for the source, refused unless it is approved and is
    still the plan that was approved."""
    source = sources.plain_name(source)
    row = record(source)
    if row is None:
        raise ApprovalRefused(f"{source}'s plan was never submitted.")
    if row["status"] != "approved":
        raise ApprovalRefused(f"{source}'s plan is {row['status'].replace('_', ' ')}, not "
                              f"approved.")
    held = planning.load(sources.folder_for(source))
    if planning.plan_hash(held) != row["plan_hash"]:
        raise ApprovalRefused(f"{source}'s plan has changed since it was approved: submit it "
                              f"again.")
    return held


def loaded_hash(source: str) -> str | None:
    """The build whose tables were last loaded for the source; None when
    none was (or the store has no plan for it)."""
    source = sources.plain_name(source)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(EXISTS, table())
        if cursor.fetchone()[0] is None:
            return None
        cursor.execute(_sql(DDL_LOADED))
        conn.commit()
        cursor.execute(_sql("SELECT loaded_hash FROM sdm.source_plans WHERE source = ?"), source)
        row = cursor.fetchone()
        return str(row[0]).strip() if row and row[0] else None
    finally:
        conn.close()


def record_load(source: str, built_hash: str) -> None:
    """Record that the build's tables are loaded for the source."""
    source = sources.plain_name(source)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(_sql(DDL_LOADED))
        cursor.execute(_sql("UPDATE sdm.source_plans SET loaded_hash = ? WHERE source = ?"),
                       built_hash, source)
        conn.commit()
    finally:
        conn.close()


def record_build(source: str, plan_hash: str, built_hash: str, *,
                 install: Callable[[], None], undo: Callable[[], None]) -> dict[str, Any]:
    """Record a build of the plan approved as `plan_hash`, in one transaction
    that holds the row, finds it approved with that hash, records the build,
    and calls `install` (which puts the built folder in place) before it
    commits. Refused, with nothing installed, when the plan is no longer
    approved or no longer that plan; `undo` takes the folder back out when
    the commit fails after it was installed."""
    source = sources.plain_name(source)
    ensure_table()
    conn = _connect()
    try:
        try:
            cursor = conn.cursor()
            row = _row(cursor, source, held=True)
            if row is None:
                raise ApprovalRefused(f"{source}'s plan was never submitted.")
            if row["status"] != "approved":
                raise ApprovalRefused(f"{source}'s plan is {row['status'].replace('_', ' ')}, "
                                      f"not approved: nothing was built.")
            if row["plan_hash"] != plan_hash:
                raise ApprovalRefused(f"{source}'s plan has changed since it was approved: "
                                      f"submit it again.")
            cursor.execute(_sql(BUILT), built_hash, datetime.now(), source, plan_hash)
            if cursor.rowcount == 0:
                raise ApprovalRefused(f"{source}'s plan has changed since it was approved: "
                                      f"submit it again.")
            install()
        except BaseException:
            conn.rollback()
            raise
        try:
            conn.commit()
        except BaseException:
            undo()
            raise
        return {"source": source, "plan_hash": plan_hash, "built_hash": built_hash}
    finally:
        conn.close()
