"""Environment-driven configuration for the SDG proof of concept.

Connection settings come from a .env file (see .env.example). Credentials are
never written into artifacts; only the server, database, and driver appear in
plans and manifests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

_loaded = False


def load_env(env_file: str | Path | None = None) -> None:
    """Load .env once. Existing process environment wins over the file."""
    global _loaded
    if _loaded and env_file is None:
        return
    path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)
    _loaded = True


def odbc_value(value: str) -> str:
    """A connection-string value that reads back as itself.

    A value holding `;`, `{` or `}`, or edged with spaces, is braced, with `}`
    doubled, as ODBC reads it; otherwise a `;` in a password or a database
    name would end the value and start another attribute. Plain values are
    left as they are."""
    if value != value.strip() or any(c in value for c in ";{}"):
        return "{" + value.replace("}", "}}") + "}"
    return value


@dataclass(frozen=True)
class MssqlSettings:
    """Everything needed to build a SQL Server ODBC connection string."""

    server: str
    port: int
    user: str
    password: str
    driver: str
    trust_cert: bool
    # Windows authentication. The common case on a domain-joined machine, and
    # the one where there is no password to store anywhere.
    trusted: bool = False
    # Encrypt the connection. Its own setting, so a server with a certificate
    # the machine trusts is encrypted and checked; unset, it follows
    # trust_cert, as it always has.
    encrypt: bool | None = None

    @property
    def endpoint(self) -> str:
        """The server as a connection reaches it: its port named unless 1433.
        Two instances on one host differ here, where their names do not."""
        return self.server if self.port == 1433 else f"{self.server},{self.port}"

    def connection_string(self, database: str) -> str:
        server = self.endpoint
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={odbc_value(server)}",
            f"DATABASE={odbc_value(database)}",
        ]
        if self.trusted:
            parts.append("Trusted_Connection=yes")
        else:
            parts.extend([f"UID={odbc_value(self.user)}",
                          f"PWD={odbc_value(self.password)}"])
        if self.trust_cert:
            parts.append("TrustServerCertificate=yes")
        encrypt = self.trust_cert if self.encrypt is None else self.encrypt
        parts.append("Encrypt=yes" if encrypt else "Encrypt=no")
        return ";".join(parts) + ";"

    def redacted(self, database: str) -> str:
        """Same string with the password removed, safe for logs and artifacts."""
        built = self.connection_string(database)
        if self.trusted or not self.password:
            return built
        return built.replace(f"PWD={odbc_value(self.password)}", "PWD=***")


@dataclass(frozen=True)
class Settings:
    mssql: MssqlSettings
    source_db: str
    ref_db: str
    # Where analysis lives: the denormalized extracts every source is read
    # into, and the tool's own tables beside them -- saved patterns, baselines,
    # generation rules, layouts. A database of its own because none of it
    # belongs to the customer whose data is being read, and because the store
    # is centralized across sources while a source database is one source.
    denorm_db: str
    # Where generated rows land. Usually the source -- synthetic rows beside
    # the real ones they were measured from is the normal case -- but naming
    # it separately is what lets a run read one database and write another.
    target_db: str
    work_dir: Path
    # The pattern store's own connection and schema. It is often on another
    # server from the sources -- a customer's denorm database, reached with a
    # login of its own, where only one schema may be written -- so each part is
    # set on its own, and each defaults to the configured server's.
    denorm: MssqlSettings | None = None
    denorm_schema: str = "sdm"

    @property
    def store(self) -> MssqlSettings:
        """The connection settings for the pattern store."""
        return self.denorm or self.mssql


def _flag(name: str, default: str = "yes") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "yes", "true", "y"}


def _setting_flag(name: str, default: bool | None = None) -> bool | None:
    """A yes/no setting, or `default` where it is not set."""
    value = os.getenv(name, "").strip()
    return default if not value else value.lower() in {"1", "yes", "true", "y"}


def _plain_schema(schema: str) -> str:
    """The store's schema name, refused unless it is a plain SQL name: it is
    written into SQL text wherever the store is read or written."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", schema):
        raise ValueError(f"SDG_DENORM_SCHEMA={schema!r} is not a plain schema name. Use "
                         "letters, digits and underscores only, then start again.")
    return schema


def get_settings(env_file: str | Path | None = None) -> Settings:
    load_env(env_file)
    mssql = MssqlSettings(
        server=os.getenv("SDG_MSSQL_SERVER", "localhost"),
        port=int(os.getenv("SDG_MSSQL_PORT", "1433")),
        user=os.getenv("SDG_MSSQL_USER", "sa"),
        password=os.getenv("SDG_MSSQL_PASSWORD", ""),
        driver=os.getenv("SDG_MSSQL_DRIVER", "ODBC Driver 18 for SQL Server"),
        trust_cert=_flag("SDG_MSSQL_TRUST_CERT"),
        trusted=_flag("SDG_MSSQL_TRUSTED", "no"),
        encrypt=_setting_flag("SDG_MSSQL_ENCRYPT"),
    )
    # The pattern store: every setting falls back to the server's own.
    denorm = MssqlSettings(
        server=os.getenv("SDG_DENORM_SERVER", mssql.server),
        port=int(os.getenv("SDG_DENORM_PORT", str(mssql.port))),
        user=os.getenv("SDG_DENORM_USER", mssql.user),
        password=os.getenv("SDG_DENORM_PASSWORD", mssql.password),
        driver=os.getenv("SDG_DENORM_DRIVER", mssql.driver),
        trust_cert=_flag("SDG_DENORM_TRUST_CERT", "yes" if mssql.trust_cert else "no"),
        trusted=_flag("SDG_DENORM_TRUSTED", "yes" if mssql.trusted else "no"),
        encrypt=_setting_flag("SDG_DENORM_ENCRYPT", mssql.encrypt),
    )
    work_dir = Path(os.getenv("SDG_WORK_DIR", "work"))
    if not work_dir.is_absolute():
        work_dir = REPO_ROOT / work_dir
    return Settings(
        mssql=mssql,
        source_db=os.getenv("SDG_DB_SOURCE", "FACETS_QA"),
        ref_db=os.getenv("SDG_DB_REF", "MDM_REF"),
        denorm_db=os.getenv("SDG_DB_DENORM", "SDM_DENORM"),
        target_db=os.getenv("SDG_DB_TARGET", os.getenv("SDG_DB_SOURCE", "FACETS_QA")),
        work_dir=work_dir,
        denorm=denorm,
        denorm_schema=_plain_schema(
            os.getenv("SDG_DENORM_SCHEMA", os.getenv("SDG_ANALYZER_SCHEMA", "sdm"))),
    )


# Whether generated rows may be written into a database. Off for a customer
# who is to have files only: everything else -- reading and profiling a
# database, patterns, files -- works as before, and nothing generated is ever
# inserted anywhere.
DATABASE_GENERATION_OFF = (
    "Generating into a database is turned off for this installation, so nothing "
    "was written. Generate a file instead. (It is turned on again with "
    "SDG_DATABASE_GENERATION=yes in the .env file.)"
)


# Tables above this many rows are never read whole when a database is read:
# their measures come from a sample, and lookups into them go by index.
WHOLE_READ_MAX_ROWS = 50_000_000


def whole_read_max_rows() -> int:
    """The row count above which a table is read through a sample, per table.

    SDG_WHOLE_READ_MAX_ROWS in the .env file; 0 reads every table whole."""
    load_env()
    raw = os.getenv("SDG_WHOLE_READ_MAX_ROWS", "").strip().replace(",", "").replace("_", "")
    try:
        return max(0, int(raw)) if raw else WHOLE_READ_MAX_ROWS
    except ValueError:
        return WHOLE_READ_MAX_ROWS


# The pairwise measures of a profile -- which columns decide others, which
# dates come first -- stop being taken after this many minutes; every table's
# column measures are still taken.
PROFILE_MAX_MINUTES = 60


def profile_max_minutes() -> float:
    """SDG_PROFILE_MAX_MINUTES in the .env file; 0 sets no limit."""
    load_env()
    raw = os.getenv("SDG_PROFILE_MAX_MINUTES", "").strip()
    try:
        return max(0.0, float(raw)) if raw else float(PROFILE_MAX_MINUTES)
    except ValueError:
        return float(PROFILE_MAX_MINUTES)


LOCAL_SERVERS = {"", "localhost", ".", "(local)", "127.0.0.1"}


def customer_machine() -> str | None:
    """Why this machine is set up as a customer's, or None for a demo machine.

    SDG_ENV=customer says so outright. Without it, a pattern store on its own
    server (SDG_DENORM_SERVER) is the customer shape: no demo machine has one.
    """
    load_env()  # a name set in the environment wins
    if os.getenv("SDG_ENV", "").strip().lower() == "customer":
        return "SDG_ENV=customer"
    server = os.getenv("SDG_DENORM_SERVER", "").strip()
    if server.lower().split("\\")[0] not in LOCAL_SERVERS:
        return f"SDG_DENORM_SERVER={server}"
    return None


def not_a_demo_machine() -> str | None:
    """Why the demo's destructive steps (dropping and rebuilding the demo
    databases) are refused here, or None on a machine that says it is for
    development or demos. Like database_generation, it asks for SDG_ENV=local
    or laptop outright: a machine set up without saying is not taken for one."""
    reason = customer_machine()
    if reason:
        return f"set up as a customer's ({reason} in .env)"
    if os.getenv("SDG_ENV", "").strip().lower() not in {"local", "laptop"}:
        return "not set up as a demo machine (SDG_ENV=local or laptop is not in .env)"
    return None


def database_generation() -> bool:
    """Whether this installation may write generated rows into a database.

    SDG_DATABASE_GENERATION in the .env file says. Without it, only a machine
    that says it is for development or demos (SDG_ENV=local or laptop) may:
    anywhere else -- a customer's, or one set up without saying -- rows are
    never written into a database unless someone turned it on."""
    load_env()
    stated = os.getenv("SDG_DATABASE_GENERATION", "").strip()
    if stated:
        return _flag("SDG_DATABASE_GENERATION")
    demo = os.getenv("SDG_ENV", "").strip().lower() in {"local", "laptop"}
    return demo and customer_machine() is None


# Databases SDM never reads or writes from its screens or its loads, whatever
# is chosen: production. SDG_PROTECTED_DATABASES in the .env file names them,
# separated by commas; no name is built in, so nothing of one customer ships to
# another. A customer's machine must name them: without the setting it refuses,
# rather than run with no production database protected. On a customer's
# machine the source it is extracted from (SDG_DB_SOURCE) is one too: the
# extract reads it, nothing else does.
PROTECTED_SETTING = "SDG_PROTECTED_DATABASES"
# Where a SQL script's guard names the protected databases: SDM writes their
# names there when it runs the script (fill_protected); a script run by hand
# stops until a person has put them there.
PROTECTED_PLACEHOLDER = "{{PROTECTED_DATABASES}}"
# A database name a guard may hold: no quote or comma, which would end it, and
# no space or line break -- the scripts' guards take spaces out of the list (a
# person writes "A, B"), so a name with one could never match.
_GUARDABLE = re.compile(r"[^',\s]{1,128}")


class ProtectedDatabase(PermissionError):
    """A database SDM must never read or write was about to be."""


def protected_databases() -> set[str]:
    load_env()
    named = {n.strip().upper() for n in os.getenv(PROTECTED_SETTING, "").split(",")} - {""}
    if any(n.startswith("<") for n in named):
        raise ProtectedDatabase(
            f"{PROTECTED_SETTING} still holds the example's placeholder: name the production "
            f"databases SDM must never write to, separated by commas, in .env, then run "
            f"again. Nothing was done.")
    customer = customer_machine()
    if customer and not named:
        raise ProtectedDatabase(
            f"This machine is set up as a customer's ({customer}), and {PROTECTED_SETTING} "
            f"is not set: name the production databases SDM must never write to, separated "
            f"by commas, in .env (for example {PROTECTED_SETTING}=PRODDB), then run again. "
            f"Nothing was done.")
    if customer:
        named.add(os.getenv("SDG_DB_SOURCE", "").strip().upper())
    return named - {""}


def fill_protected(text: str) -> str:
    """A SQL script with its guard's placeholder replaced by the protected
    databases' names, separated by commas (none on a demo machine that names
    none: the guard then stops nothing)."""
    names = sorted(protected_databases())
    for name in names:
        if not _GUARDABLE.fullmatch(name):
            raise ProtectedDatabase(
                f"{PROTECTED_SETTING} names {name!r}, which a script's guard cannot hold "
                f"(a quote, comma, space or line break in a database name). Nothing was "
                f"run.")
    return text.replace(PROTECTED_PLACEHOLDER, ",".join(names))


def refuse_protected(database: str | None, doing: str) -> None:
    """Stop before `doing` anything to a protected database."""
    if database and database.strip().upper() in protected_databases():
        raise ProtectedDatabase(
            f"{database} is a protected production database. Operation blocked: "
            f"cannot {doing} it. Select a non-production database. (Protected databases: "
            f"{PROTECTED_SETTING} in .env"
            f"{', and the source database on a customer machine' if customer_machine() else ''}.)")
