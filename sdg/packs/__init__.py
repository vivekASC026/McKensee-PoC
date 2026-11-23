"""Business packs: what one business calls its records, and what it holds.

SDM is one framework. A business pack is configuration only -- no code --
that says what a business's people and records are called, which of its
tables are reference data, what its patterns are found on and which file
formats it is offered. Health insurance is one pack; another business is
another folder beside it.

    sdg/packs/<id>/
      pack.json          entities by role, tables, plan, files, formats, modules
      features.json      what patterns are found on, counted and reported by
      story.json         the words a story or a search is read in
      glossary.json      every word the screens use for its records
      column_names.json  business names for its columns
      catalog.yaml       its element catalog
      sql/               its tables in the SDM platform database (pack.json sql)
      extract/           the SELECT behind each of those tables, per source

The active pack is SDG_PACK in .env (health when it is not set), one per
installation. Code asks the pack what an entity plays -- primary, child,
parent_of_primary -- and never tests an entity's key. A pack's keys are
stored with what was made from it, so a pack keeps its keys once in use.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

BUILT_IN = Path(__file__).resolve().parent
DEFAULT = "health"
ROLES = ("primary", "child", "parent_of_primary")
# Entity keys are stored in columns of this width (sdm.patterns.entity,
# generation_requests.subject).
KEY_WIDTH = 20


class PackError(ValueError):
    """A pack that cannot be used, said plainly."""


class PackRebuilt(Exception):
    """The running pack's folder holds another build than the one SDM read
    at start (the API answers 409 with this message): analysis waits for a
    restart, so nothing is measured under one build and read under another."""


# The record a generated pack's build leaves in its folder (the source
# accelerator's builder writes it): which build the folder holds.
BUILD_MARKER = ".sdm-build.json"


class NotOffered(Exception):
    """A format or a part of SDM the active pack does not offer (the API
    answers 404 with this message). Neither a ValueError nor a LookupError:
    nothing the person typed is wrong and no record is missing, and a route
    catching either must not take it for that."""


@dataclass(frozen=True)
class Pack:
    id: str
    folder: Path
    data: dict[str, Any]
    features: dict[str, Any] = field(default_factory=dict)
    story: dict[str, Any] = field(default_factory=dict)
    glossary: dict[str, Any] = field(default_factory=dict)
    # The build the folder held when the pack was read (BUILD_MARKER); None
    # for a pack no build wrote (health, banking).
    built_hash: str | None = None

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.id)

    # -- entities ---------------------------------------------------------

    @property
    def entities(self) -> dict[str, dict[str, Any]]:
        return self.data["entities"]

    def role(self, key: str) -> str | None:
        found = self.entities.get(key)
        return None if found is None else found["role"]

    def with_role(self, role: str) -> str | None:
        """The entity playing `role`, by its key."""
        return next((k for k, e in self.entities.items() if e["role"] == role), None)

    def child_anchors(self) -> tuple[str | None, str | None]:
        """The elements a child record and each of its lines are known by
        (health: CLAIM_ID, LINE_NUMBER; a bank: ACCOUNT_ID, TXN_SEQ)."""
        child = self.entities.get(self.child or "") or {}
        return child.get("anchor"), child.get("line_anchor")

    @property
    def primary(self) -> str:
        return self.with_role("primary")  # validated: there is always one

    @property
    def child(self) -> str | None:
        return self.with_role("child")

    def term(self, key: str, form: str = "many") -> str:
        """What the screens call an entity: one, many, One, Many, line..."""
        words = self.glossary.get(key) or {}
        return str(words.get(form) or key)

    # -- the rest ---------------------------------------------------------

    def value(self, path: str) -> Any:
        """A value by its place: `features:patterns.defined`, `pack:tables.modes`.

        A copy: whoever reads it may change theirs; the pack stays as loaded.
        """
        part, _, dotted = path.partition(":")
        node: Any = {"pack": self.data, "features": self.features, "story": self.story,
                     "glossary": self.glossary}[part]
        for step in dotted.split("."):
            if not isinstance(node, dict) or step not in node:
                raise PackError(f"The {self.id} pack has no {path}.")
            node = node[step]
        return copy.deepcopy(node)

    def has_module(self, name: str) -> bool:
        return name in (self.data.get("modules") or [])

    @property
    def formats(self) -> dict[str, list[str]]:
        return self.data.get("formats") or {}

    def sql(self, kind: str) -> list[Path]:
        """The pack's SQL scripts of a kind (store_tables, store_indexes), in order."""
        return [self.folder / name for name in (self.data.get("sql") or {}).get(kind) or []]

    @property
    def extract_folder(self) -> Path:
        """Where the pack's extract definitions are: one SELECT file per source."""
        return self.folder / "extract"

    @property
    def catalog_path(self) -> Path:
        return self.folder / "catalog.yaml"

    def column_names(self) -> dict[str, Any]:
        return _read(self.folder, "column_names.json")


def _read(folder: Path, name: str) -> dict[str, Any]:
    path = folder / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PackError(f"{path} is not valid JSON: {e}") from e


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads((BUILT_IN / "schema.json").read_text(encoding="utf-8"))


def check(data: dict[str, Any], where: str) -> None:
    """Refuse a pack.json that code could not read, saying what is wrong."""
    import jsonschema

    schema = json.loads((BUILT_IN / "schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        place = ".".join(str(p) for p in e.absolute_path) or "the top"
        raise PackError(f"{where}: {e.message} (at {place})") from e
    roles = [e["role"] for e in data["entities"].values()]
    for role in ROLES:
        if roles.count(role) > 1:
            raise PackError(f"{where}: more than one entity plays {role}.")
    if "primary" not in roles:
        raise PackError(f"{where}: no entity plays primary.")
    for key, entity in data["entities"].items():
        parent = entity.get("parent")
        if parent is not None and parent not in data["entities"]:
            raise PackError(f"{where}: {key}'s parent {parent} is not an entity.")
    for role in (data.get("plan") or {}).get("joins") or {}:
        if role not in roles:
            raise PackError(f"{where}: plan.joins names {role}, which no entity plays.")
    # Names a pack puts into SQL by name: plain ones only.
    files = data.get("files") or {}
    for column in files.get("latest_columns") or []:
        plain_name(column, f"{where}: files.latest_columns")
    for part in ("from", "suffix"):
        if files.get("composed_id"):
            plain_name(files["composed_id"][part], f"{where}: files.composed_id.{part}")


# The id pattern schema.json requires of a pack.
_PACK_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def folder_for(pack_id: str) -> Path:
    """Where a pack lives: an added one under SDG_PACKS_DIR, else a built-in one."""
    # A pack is named, not addressed: the same plain id pack.json must hold,
    # so "../x" can never reach a pack.json (and its SQL scripts) elsewhere.
    if not isinstance(pack_id, str) or not _PACK_ID.match(pack_id):
        raise PackError(f"{pack_id!r} is not a pack id: lowercase letters, digits, "
                        "hyphens or underscores, starting with a letter. "
                        "Set SDG_PACK in .env to one of the packs.")
    extra = os.getenv("SDG_PACKS_DIR", "").strip()
    for base in ([Path(extra)] if extra else []) + [BUILT_IN]:
        found = base / pack_id
        if (found / "pack.json").exists():
            return found
    known = sorted(p.parent.name for p in BUILT_IN.glob("*/pack.json"))
    raise PackError(f"No business pack {pack_id!r}. Known: {', '.join(known)}. "
                    "Set SDG_PACK in .env to one of them.")


@lru_cache(maxsize=8)
def load(pack_id: str) -> Pack:
    return read_folder(folder_for(pack_id), pack_id)


def read_folder(folder: Path, pack_id: str, pack_text: str | None = None) -> Pack:
    """The pack in `folder`, read now (load keeps what it read; a pack the
    source accelerator has just built is read again each time). With
    `pack_text`, pack.json is that text, already read and checked by the
    caller, rather than the file read again."""
    if pack_text is None:
        data = _read(folder, "pack.json")
    else:
        try:
            data = json.loads(pack_text)
        except ValueError as e:
            raise PackError(f"{folder / 'pack.json'} is not valid JSON: {e}") from e
    check(data, str(folder / "pack.json"))
    if data["id"] != pack_id:
        raise PackError(f"{folder / 'pack.json'} says it is {data['id']!r}, not {pack_id!r}.")
    # What a plan needs, where the pack does not say: no joins between record
    # kinds, no root counts (the source's own), and a plan named for the pack.
    data.setdefault("plan_name", f"{pack_id}-slice")
    plan = data.setdefault("plan", {})
    plan.setdefault("joins", {})
    plan.setdefault("root_counts", {})
    # How a pack's people are read back, where it does not say: nothing
    # composed, no latest column, no codes (schema.json files: a list empty,
    # anything else none).
    files = data.setdefault("files", {})
    for name, spec in _schema()["properties"]["files"]["properties"].items():
        files.setdefault(name, [] if spec.get("type") == "array" else None)
    return Pack(id=pack_id, folder=folder, data=data,
                features=_read(folder, "features.json"),
                story=_read(folder, "story.json"),
                glossary=_read(folder, "glossary.json"),
                built_hash=build_of(folder))


# What build_of says of a marker that is there but cannot be read: never a
# build's hash, so it never matches one.
UNREADABLE_BUILD = "unreadable"


def build_of(folder: Path) -> str | None:
    """The build a pack folder holds now, by its marker's hash; None when no
    build wrote the folder."""
    path = folder / BUILD_MARKER
    if not path.exists():
        return None
    try:
        held = json.loads(path.read_text(encoding="utf-8")).get("built_hash")
    except (OSError, ValueError, AttributeError):
        return UNREADABLE_BUILD
    return held if isinstance(held, str) and held else UNREADABLE_BUILD


# How long the store's word on the loaded build is taken as current.
STORE_LOOK_SECONDS = 10.0
_store_seen: dict[str, tuple[float, tuple[str | None, str | None]]] = {}


def loaded_build(pack_id: str) -> tuple[str | None, str | None]:
    """What the store says of a pack (sdm.source_plans): the build whose
    tables were last loaded, and the build last made; (None, None) when it
    says nothing, or cannot be read (a store that cannot be reached fails the
    analysis on its own). Looked up at most every STORE_LOOK_SECONDS."""
    import time

    seen = _store_seen.get(pack_id)
    if seen and time.monotonic() - seen[0] < STORE_LOOK_SECONDS:
        return seen[1]
    found: tuple[str | None, str | None] = (None, None)
    try:
        import pyodbc

        from sdg.analyzer.config import get_connection_string
        from sdg.config import get_settings

        table = f"{get_settings().denorm_schema}.source_plans"
        conn = pyodbc.connect(get_connection_string(), timeout=5)
        try:
            cursor = conn.cursor()
            if cursor.execute("SELECT COL_LENGTH(?, 'loaded_hash')", table).fetchone()[0]:
                row = cursor.execute(
                    f"SELECT TOP (1) loaded_hash, built_hash FROM {table} WHERE pack_id = ? "
                    f"AND loaded_hash IS NOT NULL ORDER BY built_at DESC", pack_id).fetchone()
                if row:
                    found = (str(row[0]).strip(), str(row[1]).strip() if row[1] else None)
        finally:
            conn.close()
    except Exception:  # unreadable store: nothing said from here
        found = (None, None)
    _store_seen[pack_id] = (time.monotonic(), found)
    return found


def current_build_problem() -> str | None:
    """Why analysis must wait for a restart: the running pack was written by a
    build, and its folder now holds another build (or none), or the store
    says another build's tables were loaded (on another machine, say). None
    otherwise."""
    pack = active()
    if pack.built_hash is None:
        return None
    if build_of(pack.folder) != pack.built_hash:
        return (f"The {pack.id} pack was rebuilt after SDM started. Restart SDM to analyze "
                f"under the new build.")
    loaded, latest = loaded_build(pack.id)
    if loaded and loaded != pack.built_hash:
        if latest == pack.built_hash:
            # This build is the store's latest, its tables not loaded yet.
            return (f"The {pack.id} pack's tables still hold an earlier build's rows. Load "
                    f"this build (Source setup, Load), then analyze.")
        return (f"The {pack.id} pack's tables were loaded from another build than the one SDM "
                f"runs. Put that build in the packs folder (build again, or copy it) and "
                f"restart SDM.")
    return None


def active_id() -> str:
    from sdg.config import load_env

    load_env()
    return os.getenv("SDG_PACK", "").strip() or DEFAULT


def active() -> Pack:
    """The installation's business pack."""
    return load(active_id())


def is_primary(key: Any) -> bool:
    """Whether the active pack's entity of this key is its primary record."""
    return active().role(str(key)) == "primary"


def is_child(key: Any) -> bool:
    """Whether the active pack's entity of this key is a child of its primary."""
    return active().role(str(key)) == "child"


def offers(module: str) -> bool:
    """Whether the active pack offers a module: the code of a format only one
    business uses (x12, snip, adjudication, fhir, hl7v2, hl7v3, cda). Health
    offers them all; another pack only what it names."""
    return active().has_module(module)


def require(module: str, what: str) -> None:
    """Refuse what the active pack does not offer, before anything is written."""
    if not offers(module):
        raise NotOffered(f"{what} is not offered by the active business pack "
                         f"({active().name}).")


# A table or column name a pack puts into SQL: a plain name only.
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def plain_name(name: Any, where: str) -> str:
    """A pack's table or column name, refused unless it is a plain one: it is
    written into SQL as it is, so nothing else may stand there."""
    if not isinstance(name, str) or not _PLAIN_NAME.fullmatch(name):
        raise PackError(f"{where}: {name!r} is not a plain table or column name.")
    return name


def offered(module: str, what: str):
    """A route dependency: the route answers only where the pack offers the module."""
    def dependency() -> None:
        require(module, what)
        if module == "pattern_analysis":
            problem = current_build_problem()
            if problem:
                raise PackRebuilt(problem)
    return dependency


def held(path: str) -> Any:
    """A value the active pack holds, by its place (see Pack.value)."""
    return active().value(path)


# Places only a pack about people fills: a household, a sex, a marital status,
# who holds the policy, when a record began. A pack may leave each out (a
# generated one does); the code reading it then uses held_or's default. Kept
# in schema.json beside the rest of the pack's contract.
OPTIONAL_PLACES: frozenset[str] = frozenset(_schema()["x-optional-places"])


def held_or(path: str, default: Any) -> Any:
    """A value the active pack holds, or `default` where it holds none at
    that place. Only an optional place (OPTIONAL_PLACES) may fall back: any
    other is refused, as is a pack that cannot be loaded."""
    if path not in OPTIONAL_PLACES:
        raise PackError(f"{path} is not a place a pack may leave out; read it with held.")
    pack = active()
    try:
        return pack.value(path)
    except PackError:
        return default
