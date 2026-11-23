"""Connection registry for the PoC.

Only SQL Server is supported. Connections are named ("source", "ref", "target",
"denorm")
so plans and manifests can refer to them without carrying credentials.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import pyodbc

from sdg.config import Settings, get_settings, MssqlSettings


@dataclass(frozen=True)
class ConnectionRef:
    """A named database connection. Credentials stay in the registry."""

    name: str
    database: str
    server: str
    driver: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": "mssql",
            "server": self.server,
            "database": self.database,
            "driver": self.driver,
        }


class ConnectionRegistry:
    """Resolves connection names to live pyodbc connections."""

    def __init__(self, settings: Settings | None = None, *,
                 reads_production: bool = False) -> None:
        self.settings = settings or get_settings()
        # Production is never opened, whichever role names it, except by the
        # extract: it is the one thing that reads a source SDM protects, and it
        # reads it as the source only -- never through the store it writes.
        self.reads_production = reads_production
        self._databases: dict[str, str] = {
            "source": self.settings.source_db,
            # The database the analysis store was read from. "source" follows
            # whichever database the pipeline is working on; this does not, so
            # code describing the landscape -- a pattern's existing members --
            # still finds the rows it was measured from.
            "landscape": self.settings.source_db,
            "target": self.settings.target_db,
            "ref": self.settings.ref_db,
            "denorm": self.settings.denorm_db,
            "master": "master",
        }
        # Roles pointing at a server other than the configured one.
        self._overrides: dict[str, MssqlSettings] = {}

    def register(self, name: str, database: str) -> None:
        self._databases[name] = database
        # A plain database name means "on the configured server", so any
        # override from a previous registration has to go with it.
        self._overrides.pop(name, None)

    def register_connection(self, name: str, settings: MssqlSettings, database: str) -> None:
        """Point one role at a server of its own.

        Every role resolved against one configured server until a target had to
        be somewhere else. Only the connection changes -- what the role means,
        and everything that reads it, is untouched.
        """
        self._databases[name] = database
        self._overrides[name] = settings

    def settings_for(self, name: str) -> MssqlSettings:
        if name in self._overrides:
            return self._overrides[name]
        # The pattern store has a connection of its own.
        if name == "denorm":
            return self.settings.store
        return self.settings.mssql

    def is_overridden(self, name: str) -> bool:
        return name in self._overrides

    def names(self) -> list[str]:
        return sorted(self._databases)

    def database_for(self, name: str) -> str:
        if name not in self._databases:
            raise KeyError(f"unknown connection name: {name!r} (known: {self.names()})")
        return self._databases[name]

    def ref(self, name: str) -> ConnectionRef:
        mssql = self.settings_for(name)
        return ConnectionRef(
            name=name,
            database=self.database_for(name),
            server=mssql.server,
            driver=mssql.driver,
        )

    def connection_string(self, name: str) -> str:
        return self.settings_for(name).connection_string(self.database_for(name))

    @contextlib.contextmanager
    def connect(self, name: str, *, autocommit: bool = False) -> Iterator[pyodbc.Connection]:
        if not (self.reads_production and name == "source"):
            from sdg.config import refuse_protected

            refuse_protected(self.database_for(name), "connect to")
        conn = pyodbc.connect(self.connection_string(name), autocommit=autocommit)
        try:
            yield conn
        finally:
            conn.close()

    def query(
        self, name: str, sql: str, params: Sequence[Any] = (), *, timeout: int = 0
    ) -> list[dict[str, Any]]:
        """Run a read-only query and return rows as dicts. With `timeout`, a
        query still running after that many seconds is stopped and raises."""
        with self.connect(name) as conn:
            if timeout:
                conn.timeout = timeout
            cur = conn.cursor()
            for setting in self.session_settings(name):
                cur.execute(setting)
            cur.execute(sql, *params) if params else cur.execute(sql)
            if cur.description is None:
                return []
            columns = [c[0] for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def session_settings(self, name: str) -> tuple[str, ...]:
        """Fixed session settings run on a fresh connection before a query
        (none here): a subclass's own constants, never a caller's SQL."""
        return ()

    def scalar(self, name: str, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.query(name, sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def check(self, name: str) -> dict[str, Any]:
        """Smoke test used by `sdg connect`."""
        try:
            row = self.query(
                name,
                "SELECT DB_NAME() AS db, @@SERVERNAME AS server, "
                "SERVERPROPERTY('ProductVersion') AS version",
            )[0]
            return {"connection": name, "ok": True, **{k: str(v) for k, v in row.items()}}
        except Exception as exc:  # pragma: no cover - reported, not raised
            from sdg.discovery.errors import plain

            return {"connection": name, "ok": False, "error": plain(exc)}
