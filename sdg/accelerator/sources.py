"""A source, named, and how to reach it.

A source is a name -- BANK_DEMO, say -- and its settings come from the .env
file as SDG_SOURCE_<NAME>_SERVER, _PORT, _DATABASE, _USER, _PASSWORD,
_TRUSTED, _DRIVER, _TRUST_CERT and _ENCRYPT. Each falls back to the
configured server's own SDG_MSSQL_* value, and the database to the name
itself, so a database on a demo machine's server needs no new lines at all.
On a customer's machine the login does not fall back: the source's own
_USER (with its _PASSWORD) or _TRUSTED must be set, so the store's login is
never used, unasked, to read a customer's production database.

A source is read through a SourceRegistry: a connection registry that opens
the source and nothing else, sends it only statements that read
(source_read.refuse_unless_read), each with a time limit, and never hands out
a raw connection. Discovery and profiling take it as they take any registry,
so every statement they send passes the same check. Before the first read,
registry_for asks the database what the login may do, and refuses a login
that can write unless the person said to go on.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator, Sequence

import pyodbc

from sdg.accelerator.source_read import SOURCE, query, refuse_unless_read
from sdg.config import MssqlSettings, get_settings, load_env, not_a_demo_machine
from sdg.discovery.connections import ConnectionRegistry

# A source's name goes into setting names and a folder name, so it is kept
# to what both take as they stand.
_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,31}")

# Seconds a statement may run when its caller set no limit: discovery and
# profiling pass none for some of theirs, and a source is often production.
DEFAULT_TIMEOUT = 60

# What a login that only reads may not do, asked of the database itself: on
# the database as a whole, and -- since a grant on one table is as much a
# write as a grant on all -- on any one table, each table asked in the same
# statement.
DATABASE_PERMISSIONS = ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE", "EXECUTE",
                        "CONTROL")
TABLE_PERMISSIONS = ("INSERT", "UPDATE", "DELETE", "ALTER")
ON_A_TABLE = "INSERT, UPDATE, DELETE or ALTER on a table"
WRITE_PERMISSIONS = DATABASE_PERMISSIONS + (ON_A_TABLE,)


def permission_alias(permission: str) -> str:
    """The column LOGIN_SQL answers a permission in."""
    if permission == ON_A_TABLE:
        return "can_write_a_table"
    return "can_" + permission.lower().replace(" ", "_")


_ON_ANY_TABLE = " OR ".join(
    f"HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', '{p}') = 1"
    for p in TABLE_PERMISSIONS)
LOGIN_SQL = ("SELECT " + ", ".join(
    f"HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', '{p}') AS {permission_alias(p)}"
    for p in DATABASE_PERMISSIONS)
    + ", CASE WHEN EXISTS (SELECT 1 FROM sys.tables AS t "
      "JOIN sys.schemas AS s ON s.schema_id = t.schema_id "
      f"WHERE t.is_ms_shipped = 0 AND ({_ON_ANY_TABLE})) THEN 1 ELSE 0 END "
      f"AS {permission_alias(ON_A_TABLE)}")

# Whether this thread is inside SourceRegistry.query, the one place that may
# open a connection.
_querying = threading.local()


class SourceError(ValueError):
    """A source that cannot be named, reached or given a folder."""


class WriteLogin(SourceError):
    """A login that can write to the source, refused until the person says to go on."""


class SourceRegistry(ConnectionRegistry):
    """A registry that opens the source only, and only to read it.

    The store, a target, the reference database: none of them is reachable
    from here, so nothing the accelerator runs can write a row anywhere. A
    statement that does not read is refused before it reaches the driver, and
    no connection is handed out to run anything else on."""

    # What check_login found, once registry_for has asked.
    login: dict[str, Any] | None = None

    def register(self, name: str, database: str) -> None:
        raise SourceError("a source registry holds its source only: use register_source")

    def register_connection(self, name: str, settings: MssqlSettings, database: str) -> None:
        raise SourceError("a source registry holds its source only: use register_source")

    def register_source(self, settings: MssqlSettings, database: str) -> None:
        """Point the registry at the source, under the one role it opens."""
        ConnectionRegistry.register_connection(self, SOURCE, settings, database)

    def connect(self, name: str, *,
                autocommit: bool = False) -> contextlib.AbstractContextManager[pyodbc.Connection]:
        _only_the_source(name)
        if not getattr(_querying, "inside", False):
            raise SourceError("a source is read through query only; no connection is handed out")
        return super().connect(name, autocommit=autocommit)

    def query(self, name: str, sql: str, params: Sequence[Any] = (), *,
              timeout: int = 0) -> list[dict[str, Any]]:
        _only_the_source(name)
        refuse_unless_read(sql)
        _querying.inside = True
        try:
            return super().query(name, sql, params, timeout=timeout or DEFAULT_TIMEOUT)
        finally:
            _querying.inside = False

    @contextlib.contextmanager
    def lock_timeout(self, milliseconds: int) -> Iterator[None]:
        """Within the block, each statement this thread sends gives up after
        waiting `milliseconds` on another session's lock. Set on the
        connection by the registry itself (session_settings), so the
        statement sent stays one plain read."""
        if not isinstance(milliseconds, int) or isinstance(milliseconds, bool) or milliseconds < 0:
            raise ValueError("a lock timeout is a whole number of milliseconds")
        before = getattr(_querying, "lock_timeout_ms", None)
        _querying.lock_timeout_ms = milliseconds
        try:
            yield
        finally:
            _querying.lock_timeout_ms = before

    def session_settings(self, name: str) -> tuple[str, ...]:
        held = getattr(_querying, "lock_timeout_ms", None)
        return () if held is None else (f"SET LOCK_TIMEOUT {int(held)}",)


def _only_the_source(name: str) -> None:
    if name != SOURCE:
        raise SourceError(f"the accelerator reads its source only, never {name!r}")


def plain_name(name: str) -> str:
    """The source's name, upper case, or refused when it is not a plain one."""
    upper = (name or "").strip().upper()
    if not _NAME.fullmatch(upper):
        raise SourceError(
            f"{name!r} is not a source name. Use a letter, then letters, digits or "
            f"underscores, 32 at most: BANK_DEMO, say.")
    return upper


def _setting(name: str, part: str, fallback: str) -> str:
    value = os.getenv(f"SDG_SOURCE_{name}_{part}", "").strip()
    return value or fallback


def _yes(value: str) -> bool:
    return value.strip().lower() in {"1", "yes", "true", "y"}


def source_settings(name: str) -> tuple[MssqlSettings, str]:
    """The source's connection settings and its database."""
    name = plain_name(name)
    load_env()
    base = get_settings().mssql
    port = _setting(name, "PORT", str(base.port))
    try:
        port_number = int(port)
    except ValueError as exc:
        raise SourceError(f"SDG_SOURCE_{name}_PORT={port!r} is not a port number") from exc
    encrypt = os.getenv(f"SDG_SOURCE_{name}_ENCRYPT", "").strip()
    user = os.getenv(f"SDG_SOURCE_{name}_USER", "").strip()
    trusted = os.getenv(f"SDG_SOURCE_{name}_TRUSTED", "").strip()
    customer = not_a_demo_machine()
    if customer and not user and not _yes(trusted):
        raise SourceError(
            f"This machine is {customer}, so the source's own login is required: set "
            f"SDG_SOURCE_{name}_USER and SDG_SOURCE_{name}_PASSWORD, or "
            f"SDG_SOURCE_{name}_TRUSTED=yes, in .env. The configured SDG_MSSQL_* login "
            f"is not used to read a source here.")
    if customer and not os.getenv(f"SDG_SOURCE_{name}_SERVER", "").strip():
        raise SourceError(
            f"This machine is {customer}, so the source's server is required: set "
            f"SDG_SOURCE_{name}_SERVER in .env. The source's login is never sent to the "
            f"configured SDG_MSSQL_* server unasked.")
    if customer:
        # Nothing of the store's login is borrowed: the source's own, or Windows.
        login = {"user": user, "password": os.getenv(f"SDG_SOURCE_{name}_PASSWORD") or "",
                 "trusted": _yes(trusted) if trusted else False}
    else:
        login = {"user": user or base.user,
                 # Read from the environment, never written anywhere.
                 "password": os.getenv(f"SDG_SOURCE_{name}_PASSWORD") or base.password,
                 "trusted": _yes(trusted) if trusted else base.trusted}
    settings = MssqlSettings(
        server=_setting(name, "SERVER", base.server),
        port=port_number,
        user=login["user"],
        password=login["password"],
        driver=_setting(name, "DRIVER", base.driver),
        # On a customer's machine the store's TLS choices are not borrowed: the
        # certificate is checked and the connection encrypted unless the source
        # says otherwise.
        trust_cert=_yes(_setting(name, "TRUST_CERT",
                                 "no" if customer else ("yes" if base.trust_cert else "no"))),
        trusted=login["trusted"],
        encrypt=_yes(encrypt) if encrypt else (True if customer else base.encrypt),
    )
    database = _setting(name, "DATABASE", name)
    explicit = os.getenv(f"SDG_SOURCE_{name}_DATABASE", "").strip()
    from sdg.config import protected_databases

    if not explicit and database.upper() in protected_databases():
        # A protected database is read only when the person named it a source
        # in .env, never because a name typed on the screen matched it.
        raise SourceError(
            f"{database} is a protected database: name it as a source in .env "
            f"(SDG_SOURCE_{name}_DATABASE={database}) to read it.")
    return settings, database


def check_login(registry: SourceRegistry, *, allow_write_login: bool = False) -> dict[str, Any]:
    """Ask the database what the login may do there, through the checked
    query path, and refuse a login that can write unless `allow_write_login`.
    What was found, and whether the person went on, is returned for the
    landscape to keep."""
    rows = query(registry, LOGIN_SQL)
    if not rows:
        raise SourceError("the source did not say what this login may do, so it was not read")
    held = {p: bool(rows[0].get(permission_alias(p))) for p in WRITE_PERMISSIONS}
    can = [p for p, yes in held.items() if yes]
    if can and not allow_write_login:
        raise WriteLogin(
            f"This login can {', '.join(can)} in the source. The accelerator only reads, "
            f"but a login that only reads is the safe one: ask for a read-only login (db_datareader), "
            f"or pass --allow-write-login to go on with this one.")
    return {"permissions": held, "can_write": bool(can), "allowed_write_login": bool(can)}


def registry_for(name: str, *, allow_write_login: bool = False) -> SourceRegistry:
    """A registry reading the named source, one source per run, its login
    checked before anything else is read (check_login; the result is on
    `registry.login`).

    It reads production when the source is production: reading is what the
    accelerator is for, and the source role is the one role a registry that
    reads production will open (as the extract does)."""
    settings, database = source_settings(name)
    registry = SourceRegistry(reads_production=True)
    registry.register_source(settings, database)
    registry.login = check_login(registry, allow_write_login=allow_write_login)
    return registry


def folder_for(name: str) -> Path:
    """work/accelerator/<NAME>/, open to the signed-in account only: what is
    written there describes a customer's database.

    The folder is made fresh where it can be, limited to the account, and only
    then handed back to be written in. One that is there already is taken
    only when the signed-in account owns it; and the folder, or the
    accelerator folder holding it, is refused when it is a link or a junction
    to somewhere else -- checked again right before its access is set."""
    parent = get_settings().work_dir / "accelerator"
    folder = parent / plain_name(name)
    for path in (parent, folder):
        _refuse_redirected(path)
    parent.mkdir(parents=True, exist_ok=True)
    _refuse_redirected(parent)
    try:
        folder.mkdir(mode=0o700)
        fresh = True
    except FileExistsError:
        fresh = False
    _refuse_redirected(folder)
    if not folder.is_dir():
        raise SourceError(f"{folder} is there but is not a folder")
    if not fresh and not _owned_by_this_account(folder):
        raise SourceError(
            f"{folder} is there already and the signed-in account does not own it, so "
            f"nothing was written in it. Remove it, or move it aside, and run again.")
    # Checked once more right before the access is set, so the account's
    # access is never set on wherever a link made in between points.
    _refuse_redirected(folder)
    # Limited again on every use, not only when made: access loosened since
    # is taken back before anything is written in it.
    if os.name == "nt":
        _owner_only(folder)
    else:
        os.chmod(folder, stat.S_IRWXU)
    return folder


def _refuse_redirected(path: Path) -> None:
    if _redirected(path):
        raise SourceError(f"{path} is a link or a junction; it must be a plain folder")


def _owned_by_this_account(folder: Path) -> bool:
    """Whether the signed-in account owns the folder: on Windows by its
    owner's SID, elsewhere by its owner's user id."""
    if os.name != "nt":
        return os.lstat(folder).st_uid == os.getuid()
    return _owner_sid(folder) == _account_sid(folder)


def _owner_sid(path: Path) -> str:
    """The SID of the path's owner, asked of Windows itself."""
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION
    failed = advapi32.GetNamedSecurityInfoW(
        ctypes.c_wchar_p(str(path)), 1, 1, ctypes.byref(owner), None, None, None,
        ctypes.byref(descriptor))
    if failed:
        raise SourceError(f"Could not tell who owns {path} (Windows error {failed}), so "
                          f"nothing was written in it")
    try:
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise SourceError(f"Could not read the owner of {path} (Windows error "
                              f"{ctypes.get_last_error()}), so nothing was written in it")
        try:
            return text.value or ""
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def _redirected(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _system_tool(name: str) -> str:
    """A Windows tool by its full path, so no folder on PATH can stand in for it."""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    tool = Path(root) / "System32" / name
    if not tool.is_file():
        raise SourceError(f"{tool} is not there, so the folder could not be limited to "
                          f"the signed-in account")
    return str(tool)


# The signed-in account's SID, asked of Windows once a process.
_SID: list[str] = []


def _account_sid(folder: Path) -> str:
    """The signed-in Windows account's SID, by whoami (asked once a process)."""
    if _SID:
        return _SID[0]
    who = subprocess.run([_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, check=False)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip().strip('"') if who.returncode == 0 else ""
    if not sid.startswith("S-1-"):
        raise SourceError(
            f"Could not tell which Windows account is signed in, so {folder} was not "
            f"limited to it: {(who.stderr or who.stdout).strip()}")
    _SID.append(sid)
    return sid


def _owner_only(folder: Path) -> None:
    """Remove the folder's inherited access and grant the signed-in account,
    named by its SID, full control of it and of everything made in it.
    Refuses if that fails, as the saved connection's file does."""
    sid = _account_sid(folder)
    done = subprocess.run(
        [_system_tool("icacls.exe"), str(folder), "/inheritance:r", "/grant:r",
         f"*{sid}:(OI)(CI)F"],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SourceError(
            f"Could not limit {folder} to the signed-in account: "
            f"{(done.stderr or done.stdout).strip()}")
