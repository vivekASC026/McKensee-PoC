"""Pipeline operations behind the API.

The command line and the web interface do the same work through the same
modules; this is the shared layer between them. It owns the working directory,
knows which artifact each step writes, and turns the results into the merged
shapes a screen actually needs.

Decisions are written to the same `review-decisions.json` the CLI reads, so a
run can be started in the browser and finished at a terminal, or the other way
round.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdg import dpapi
from sdg.catalog.loader import load_catalog
from sdg.catalog.model import Catalog
from sdg.config import get_settings, Settings, MssqlSettings
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import PhysicalModel
from sdg.discovery.run import discover as run_discovery
from sdg.mapping.scorer import MappingStore, map_columns
from sdg.packs import held
from sdg.plan.build import (
    DEFAULT_TABLE_MODES, BuildInputs, build_plan, default_reference_sets,
)
from sdg.plan.detect import SumExpression, detect_sum_expressions
from sdg.plan.editing import check, check_fits, coerce
from sdg.plan.model import Plan
from sdg.plan.proposer import Proposal, propose
from sdg.plan.refsets import load_for_plan
from sdg.plan.validator import blocking
from sdg.profiling.model import ProfileReport
from sdg.profiling.run import profile as run_profiling
from sdg.review.queue import build_queue, load_decisions, save_decisions
from sdg.writers.manifest import merge_manifests, build_manifest, load_manifest, save_manifest
from sdg.writers.sqlserver import (
    PartialWrite,
    WrongDatabase,
    check_cleanup_target,
    cleanup as run_cleanup,
    trigger_side_effects,
    write,
)
from sdg.writers.validate import validate_run

log = logging.getLogger(__name__)

ARTIFACTS = {
    "connections": "connections.json",
    "model": "physical-model.json",
    "profile": "profile.json",
    "mappings": "mappings.json",
    "proposals": "proposals.json",
    "sums": "measured-sums.json",
    "decisions": "review-decisions.json",
    "settings": "ui-settings.json",
    "plan": "plan.json",
    "manifest": "run-manifest.json",
}

# What a run does with a table: write rows, read its keys, or leave it alone.
TABLE_MODES = ("generate", "reuse", "exclude")

DEFAULT_SETTINGS: dict[str, Any] = {
    "rootCounts": held("pack:plan.root_counts"),
    "modes": dict(DEFAULT_TABLE_MODES),
    "drivingParents": {},
    "subjects": {},
    # Children per parent, where someone replaced what was measured.
    "ratios": {},
    "seed": "demo-seed-001",
    # It keys the seed hash: the pack's, fixed (health: facets-slice).
    "planName": held("pack:plan_name"),
}


class ServiceError(RuntimeError):
    """Something the caller can fix, phrased for a person."""


def _refuse_protected(database: str | None, doing: str) -> None:
    from sdg.config import ProtectedDatabase, refuse_protected

    try:
        refuse_protected(database, doing)
    except ProtectedDatabase as e:
        raise ServiceError(str(e)) from e


@dataclass
class Cached:
    """Loaded artifacts, kept only as long as the files behind them are unchanged."""

    stamps: dict[str, float] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, path: Path, load):
        if not path.exists():
            self.values.pop(key, None)
            self.stamps.pop(key, None)
            return None
        stamp = path.stat().st_mtime_ns
        if self.stamps.get(key) != stamp:
            self.values[key] = load(path)
            self.stamps[key] = stamp
        return self.values[key]

    def clear(self) -> None:
        self.stamps.clear()
        self.values.clear()


def attributes_filtered_on(plan) -> dict[str, set[str]]:
    """Which columns of which reference set the plan narrows against.

    A reference field can name an attribute of its parent -- the provider's
    specialty, the diagnosis chapter -- and narrow to the rows that match. The
    attribute has to be loaded with the key set for that to be possible, and
    nothing else in the plan says which ones are wanted.
    """
    wanted: dict[str, set[str]] = {}
    from sdg.plan.model import points_at

    for table in plan.tables:
        for field in table.fields:
            attribute = field.params.get("filter_attribute")
            parent = points_at(field.params, field.generator)
            if attribute and parent:
                wanted.setdefault(str(parent), set()).add(str(attribute))
            # A cohort's override may filter where the field itself does not.
            for override in field.overrides or []:
                rule = getattr(override, "params", None) or {}
                target = (points_at(rule, getattr(override, "generator", "")) or parent)
                if rule.get("filter_attribute") and target:
                    wanted.setdefault(str(target), set()).add(str(rule["filter_attribute"]))
    return wanted


class Service:
    def __init__(
        self,
        work_dir: Path | None = None,
        settings: "Settings | None" = None,
    ) -> None:
        # Settings can be handed in so one process can work on more than one
        # database: onboarding a new one is the same pipeline pointed somewhere
        # else, not a second installation.
        self.settings = settings or get_settings()
        self.work = Path(work_dir) if work_dir else self.settings.work_dir
        self.work.mkdir(parents=True, exist_ok=True)
        self.registry = ConnectionRegistry(self.settings)
        # A target chosen on screen outlives the process that chose it, so it
        # is read back here rather than only applied when the button is
        # pressed. `.env` names the default; this is the override.
        self._apply_target()
        self._cache = Cached()
        self._catalog: Catalog | None = None
        self._export_run: tuple[Any, Any, Any] | None = None
        self._coverage_plans: dict[str, dict[str, Any]] | None = None

    # Files that belong to the installation rather than to one database. The
    # rest -- schema, profile, mappings, decisions, plan, and the manifest of
    # what was written -- belong to the database they were made from.
    # files-source.json names the folder of files worked on, which belongs to
    # no one database.
    STAYS = ("ui-settings.json", "target-connection.json", "server.log",
             "working-database.txt", "files-source.json", "layout-approval.json",
             "layout-approval-log.jsonl", "unsaved-sample-purge.jsonl")
    # Names the database the files in work/ were made from.
    MARKER = "working-database.txt"

    def _working_key(self) -> str:
        """A folder name for the database the pipeline is working on."""
        server = self.registry.settings_for("target").server
        database = self.registry.database_for("target")
        return self._key_for(server, database)

    @staticmethod
    def _key_for(server: str, database: str) -> str:
        import re

        return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{server}__{database}")

    def _files_belong_to(self) -> str:
        """Which database the files now in work/ were made from.

        Recorded from now on. Before it was, every read step read the source
        named in `.env`, so files with no record are that database's.
        """
        marker = self.work / self.MARKER
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        return self._key_for(self.settings.mssql.server, self.settings.source_db)

    def _follow_target(self, previous_key: str | None = None) -> None:
        """Make every step read the database generation writes into.

        Choosing a database is choosing what the whole pipeline works on:
        reading its structure, measuring it, the reference rows a plan points
        at, the rows written and the cleanup of them. Reading one database and
        writing a plan built for it into another put rows into tables shaped
        for something else.

        The work done on the previous database is set aside under
        work/databases/<it>, and whatever was done on this one before comes
        back. A run's manifest above all must never follow: cleanup deletes
        exactly the keys it lists, from whichever database it is pointed at.
        """
        if self.registry.is_overridden("target"):
            self.registry.register_connection(
                "source", self.registry.settings_for("target"),
                self.registry.database_for("target"),
            )
        else:
            self.registry.register("source", self.registry.database_for("target"))

        current = self._working_key()
        if previous_key is None:
            previous_key = self._files_belong_to()
        if previous_key == current:
            (self.work / self.MARKER).write_text(current, encoding="utf-8")
            return
        shelf = self.work / "databases"
        away = shelf / previous_key
        away.mkdir(parents=True, exist_ok=True)
        for item in list(self.work.iterdir()):
            if not item.is_file() or item.name in self.STAYS or item.name.startswith("landscape-"):
                continue
            item.replace(away / item.name)
        back = shelf / current
        if back.exists():
            for item in list(back.iterdir()):
                if item.is_file():
                    item.replace(self.work / item.name)
        (self.work / self.MARKER).write_text(current, encoding="utf-8")
        # Called from the constructor too, before these exist.
        if hasattr(self, "_cache"):
            self._cache.clear()
        self._export_run = None
        self._coverage_plans = None

    def onboarded(self) -> list[dict[str, Any]]:
        """Every database onboarded here: the one being worked on, and the rest.

        A database is onboarded once it has been read -- its schema is on file.
        Whether rows written into it are still there matters for offboarding, so
        it is said: a manifest that has not been cleaned up lists them.
        """
        current = self._working_key()
        found: list[dict[str, Any]] = []

        def describe(key: str, folder: Path, is_current: bool) -> None:
            if not (folder / ARTIFACTS["model"]).exists():
                return
            server, _, database = key.partition("__")
            found.append({
                "key": key, "server": server, "database": database,
                "current": is_current,
                "planned": (folder / ARTIFACTS["plan"]).exists(),
                "rowsInIt": (folder / ARTIFACTS["manifest"]).exists(),
            })

        describe(current, self.work, True)
        shelf = self.work / "databases"
        if shelf.exists():
            for folder in sorted(shelf.iterdir()):
                if folder.is_dir() and folder.name != current:
                    describe(folder.name, folder, False)
        return found

    def offboard(self, key: str) -> dict[str, Any]:
        """Forget a database: its schema, profile, mappings, decisions and plan.

        Patterns are untouched. They come from the sources and live in the
        analysis store, whichever databases are onboarded to generate into.

        Refused while rows generated into it are still there: the manifest is
        what cleanup removes exactly those rows by, and forgetting it would leave
        them with nothing to say which they are. Offboarding the database being
        worked on moves work back to the one named in `.env`.
        """
        import shutil

        current = self._working_key()
        # Only a database listed as onboarded: the key names a folder that is
        # removed, and one built from anything else -- "", "..", a path --
        # would remove the shelf, work/ or the install, manifests and all.
        if key not in {d["key"] for d in self.onboarded()}:
            raise ServiceError(f"{key.partition('__')[2] or key or 'That'} is not onboarded here.")
        shelf = (self.work / "databases").resolve()
        folder = self.work if key == current else self.work / "databases" / key
        if key != current and folder.resolve().parent != shelf:
            raise ServiceError(f"{key} is not onboarded here.")
        if (folder / ARTIFACTS["manifest"]).exists():
            raise ServiceError(
                f"{key.partition('__')[2]} still holds rows generated into it. "
                f"Clean them up first (DB Generation), then offboard it."
            )

        if key == current:
            # Back to the configured database first, so its files return and
            # this one's land on the shelf -- where they are then removed.
            default = self._key_for(self.settings.mssql.server, self.settings.target_db)
            if default != current:
                try:
                    configured = self.target_connection()
                except (OSError, ValueError):
                    # A saved connection this account cannot read is cleared
                    # all the same: offboarding goes back to the .env database.
                    configured = True
                if configured:
                    self.clear_target_connection()
                else:
                    self.set_target(self.settings.target_db)
                folder = self.work / "databases" / key
            else:
                for item in list(self.work.iterdir()):
                    if item.is_file() and item.name not in self.STAYS \
                            and not item.name.startswith("landscape-"):
                        item.unlink()
                self._cache.clear()
                return {"offboarded": key, "workingOn": self.working_database()}

        if folder.resolve().parent != shelf or (folder / ARTIFACTS["manifest"]).exists():
            raise ServiceError(f"{key} is not a shelved database's folder; nothing removed.")
        try:
            shutil.rmtree(folder)
        except OSError as e:
            # A file held open (common on Windows) leaves the folder behind,
            # and it would still be listed as onboarded.
            self._cache.clear()
            log.warning("Could not remove %s: %s", folder, e)
            raise ServiceError(
                f"{key} could not be removed completely: {e.strerror or e}. "
                f"Close anything using its files, then offboard it again."
            ) from e
        self._cache.clear()
        return {"offboarded": key, "workingOn": self.working_database()}

    def working_database(self) -> dict[str, Any]:
        """What the pipeline reads and writes, for the screen to name."""
        mssql = self.registry.settings_for("target")
        return {"database": self.registry.database_for("target"), "server": mssql.server}

    def _apply_target(self) -> None:
        """Point `target` at whatever was last chosen, if anything was.

        Quietly tolerant of a missing or unreadable file: this runs in the
        constructor, and a tool that cannot start because a preference is
        malformed is worse than one that starts on its defaults.
        """
        try:
            configured = self.target_connection()
        except (OSError, ValueError):
            # Started on the defaults, but said: otherwise the pipeline points
            # at the .env database with nothing to explain why.
            log.warning("Ignoring the saved target connection: "
                        "target-connection.json could not be read.", exc_info=True)
            configured = None
        if configured:
            self.registry.register_connection(
                "target", _mssql_from(configured, self.settings),
                str(configured["database"]),
            )
            self._follow_target()
            return
        try:
            chosen = (self.read_settings() or {}).get("targetDb")
        except (OSError, ValueError, TypeError):
            log.warning("Ignoring the chosen target database: the saved "
                        "settings could not be read.", exc_info=True)
            chosen = None
        if chosen and chosen != self.registry.database_for("target"):
            self.registry.register("target", str(chosen))
        self._follow_target()

    def target_connection(self) -> dict[str, Any] | None:
        """The target connection a person configured, if they configured one.

        Kept beside the other working state rather than in `.env`, because it
        is one installation's choice rather than the installation's
        configuration, and because it can be changed without a restart.
        """
        path = self.work / "target-connection.json"
        if not path.exists():
            return None
        import json

        stored = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError(f"{path.name} is not a saved connection.")
        if stored.get("password_protected"):
            # Encrypted for this Windows account on this machine (DPAPI). A
            # ValueError here (another account or machine) falls back to .env.
            stored["password"] = dpapi.unprotect(stored.pop("password_protected"))
        elif stored.get("password") and dpapi.available():
            # Saved in plain text before passwords were encrypted: rewritten
            # encrypted the first time it is read. A failure to rewrite is
            # logged; the connection is still used.
            try:
                _write_private(path, json.dumps(_protected(stored), indent=2))
            except (OSError, ServiceError):
                log.warning("%s could not be rewritten with its password encrypted",
                            path.name, exc_info=True)
        return stored

    def test_target_connection(self, config: dict[str, Any]) -> dict[str, Any]:
        """Reach a connection without saving it.

        Separate from saving on purpose. Somebody filling in a server and a
        password wants to know it works before it is written anywhere.
        """
        from sdg.discovery.connections import ConnectionRegistry

        database = str(config.get("database") or "").strip()
        if not database:
            raise ServiceError("Name a database.")
        _refuse_protected(database, "connect to")

        probe = ConnectionRegistry(self.settings)
        probe.register_connection(
            "probe", _mssql_from(config, self.settings), database
        )
        check = probe.check("probe")
        if not check.get("ok"):
            raise ServiceError(
                f"Cannot reach {database} on "
                f"{config.get('server') or self.settings.mssql.server}: "
                f"{check.get('error', 'unknown error')}"
            )
        return check

    def save_target_connection(self, config: dict[str, Any]) -> dict[str, Any]:
        """Check it, then keep it, then use it."""
        import json

        server = (config.get("server") or "").strip()
        if not server or server.lower() == self.settings.store.server.lower():
            self._refuse_store(str(config.get("database") or ""))
        check = self.test_target_connection(config)

        stored = {
            "server": (config.get("server") or "").strip() or self.settings.mssql.server,
            "port": int(config.get("port") or self.settings.mssql.port),
            "database": str(config["database"]).strip(),
            "trusted": bool(config.get("trusted")),
            "user": (config.get("user") or "").strip(),
            # Encrypted for this Windows account before it is written (DPAPI,
            # _protected); off Windows, an owner-only file. Windows
            # authentication avoids it entirely and is why it is offered first.
            "password": "" if config.get("trusted") else (config.get("password") or ""),
            "driver": (config.get("driver") or "").strip() or self.settings.mssql.driver,
            "trustCert": bool(config.get("trustCert", True)),
        }
        path = self.work / "target-connection.json"
        _write_private(path, json.dumps(_protected(stored), indent=2))

        before = self._working_key()
        self.registry.register_connection(
            "target", _mssql_from(stored, self.settings), stored["database"]
        )
        self._follow_target(before)
        self._cache.clear()
        return {
            "target": stored["database"],
            "server": stored["server"],
            "trusted": stored["trusted"],
            **check,
        }

    def clear_target_connection(self) -> dict[str, Any]:
        """Back to the database named in `.env`, on the configured server."""
        path = self.work / "target-connection.json"
        path.unlink(missing_ok=True)
        before = self._working_key()
        self.registry.register("target", self.settings.target_db)
        self._follow_target(before)
        self._cache.clear()
        return {"target": self.settings.target_db, "server": self.settings.mssql.server}


    def _refuse_store(self, database: str) -> None:
        """The pattern store is this tool's own data: generated rows never go there."""
        if database.strip().upper() == (self.settings.denorm_db or "").upper():
            raise ServiceError(
                f"{database.strip()} is the pattern store. Generated data is never "
                "written there. Select another database."
            )

    def set_target(self, database: str) -> dict[str, Any]:
        """Choose where generated rows go, and check it can be reached.

        Checked before it is saved. A target that cannot be connected to is a
        typo nine times out of ten, and finding out at generate time means
        finding out after a plan has been built against it.
        """
        database = (database or "").strip()
        if not database:
            raise ServiceError("Name a database to generate into.")
        _refuse_protected(database, "work on")
        self._refuse_store(database)

        previous = self.registry.database_for("target")
        before = self._working_key()
        self.registry.register("target", database)
        check = self.registry.check("target")
        if not check.get("ok"):
            self.registry.register("target", previous)
            raise ServiceError(
                f"Cannot reach {database}: {check.get('error', 'unknown error')}"
            )

        self.write_settings({"targetDb": database})
        self._follow_target(before)
        return {"target": database, **check}

    def databases(self) -> list[dict[str, Any]]:
        """Every database on this server, with what the tool makes of it.

        Offered as a list rather than a text box because the names are already
        known and a typo here is a confusing failure three steps later.
        """
        rows = self.registry.query(
            "master",
            "SELECT name, database_id, state_desc FROM sys.databases "
            "WHERE database_id > 4 ORDER BY name",
        )
        from sdg.config import protected_databases

        source = self.settings.source_db
        denorm = self.settings.denorm_db
        target = self.registry.database_for("target")
        protected = protected_databases()
        out = []
        for row in rows:
            name = row["name"]
            # The analysis store is left out. It is this tool's own working
            # data -- the extracts, the saved patterns, the baselines -- and
            # generating into it would put synthetic rows beside the
            # measurements the next analysis reads. Nobody should have to
            # notice that in a dropdown and avoid it.
            if name == denorm:
                continue
            out.append({
                "name": name,
                "online": row["state_desc"] == "ONLINE",
                "isSource": name == source,
                "isStore": False,
                "isTarget": name == target,
                "protected": name.upper() in protected,
            })
        return out

    # -- paths -----------------------------------------------------------

    def path(self, key: str) -> Path:
        return self.work / ARTIFACTS[key]

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    # -- artifacts -------------------------------------------------------

    @property
    def catalog(self) -> Catalog:
        """The catalog, with each code set's values taken from the pattern store.

        A code set -- procedure codes, diagnosis codes -- is a fact about the
        sources, held in the store, not about whichever database is being
        generated into. Where the store's description holds an element the
        catalog marks `code_set`, that element's rule is a lookup of the
        store's codes in the proportions they occur. A target's own declared
        reference to a code table still comes first; that is structure, and
        the plan applies it before any catalog rule.
        """
        if self._catalog is None:
            self._catalog = load_catalog()
        path = self._landscape_path() if hasattr(self, "work") else None
        key = (str(path), path.stat().st_mtime) if path is not None else ("none",)
        # Compared against a value that is never a real key, so the first call
        # always builds -- with no store description too.
        if key != getattr(self, "_code_sets_key", ("unset",)):
            self._code_sets_key = key
            self._with_code_sets = self._catalog
            if path is not None:
                from sdg.landscape import load

                shape = load(path)
                overlay = {}
                for element in self._catalog.elements:
                    if not getattr(element, "code_set", False):
                        continue
                    held = shape.element(element.name)
                    if held is None or not held.values:
                        continue
                    overlay[element.name] = element.model_copy(update={
                        "rule": "generate", "generator": "lookup",
                        "params": {"values": [v["value"] for v in held.values],
                                   "weights": [v["weight"] for v in held.values]},
                    })
                if overlay:
                    self._with_code_sets = self._catalog.model_copy(update={
                        "elements": [overlay.get(e.name, e) for e in self._catalog.elements],
                    })
        return self._with_code_sets

    @property
    def model(self) -> PhysicalModel | None:
        return self._cache.get("model", self.path("model"), PhysicalModel.load)

    @property
    def report(self) -> ProfileReport | None:
        return self._cache.get("profile", self.path("profile"), ProfileReport.load)

    @property
    def mappings(self) -> MappingStore | None:
        return self._cache.get("mappings", self.path("mappings"), MappingStore.load)

    @property
    def plan(self) -> Plan | None:
        return self._cache.get("plan", self.path("plan"), Plan.load)

    @property
    def manifest(self) -> dict[str, Any] | None:
        return self._cache.get("manifest", self.path("manifest"), load_manifest)

    def combinations(self, refresh: bool = False) -> list[Any]:
        """The pattern store's groups of dependent features; see sdg.plan.combinations.

        Measured once per process: a few seconds of aggregate queries, and the
        store changes only when it is refreshed.
        """
        from sdg.plan.combinations import discover

        if refresh or getattr(self, "_combinations", None) is None:
            self._combinations = discover(self.registry)
        return self._combinations

    def plan_stale(self) -> bool:
        """Whether something the plan was built from has changed since.

        A rule decided in Review, a pattern applied, a row count changed: each
        is written to a file the plan reads, and the plan on disk still says
        what it said before. Comparing when they were written is enough to
        know, without building anything.
        """
        plan = self.path("plan")
        if not plan.exists():
            return False
        from sdg.plan.build import BUILDER_REVISION

        try:
            made_by = self.plan.meta.builder
        except Exception:
            # A plan that cannot be read is no plan to run.
            return True
        if made_by != BUILDER_REVISION:
            return True
        built = plan.stat().st_mtime
        return any(
            self.path(key).exists() and self.path(key).stat().st_mtime > built
            for key in ("model", "profile", "mappings", "proposals", "decisions", "settings")
        )

    def require_model(self) -> PhysicalModel:
        model = self.model
        if model is None:
            raise ServiceError("Read the database first: no schema has been discovered.")
        return model

    def require_mappings(self) -> MappingStore:
        mappings = self.mappings
        if mappings is None:
            raise ServiceError("Map the columns first: no mappings have been made.")
        return mappings

    def require_plan(self) -> Plan:
        plan = self.plan
        if plan is None:
            raise ServiceError("Build the plan first: there is nothing to run.")
        return plan

    # -- settings and decisions ------------------------------------------

    def read_settings(self) -> dict[str, Any]:
        import json

        path = self.path("settings")
        if not path.exists():
            stored = {}
        else:
            stored = json.loads(path.read_text(encoding="utf-8"))
        return {
            **DEFAULT_SETTINGS,
            **stored,
            # Both of these live in the decisions file rather than here, so the
            # command line sees them. The defaults still show through for any
            # table nobody has decided about.
            "modes": {**DEFAULT_SETTINGS["modes"], **self._read_table_setting("mode")},
            "ratios": self._read_table_setting("ratio"),
        }

    def write_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        import json

        # Both are written where the command line reads them, so a run started
        # in a browser can be finished at a prompt. Neither is kept in the
        # settings file, which only the interface reads.
        if "ratios" in values:
            self._write_table_setting("ratio", self._checked_ratios(values["ratios"]))
            values = {k: v for k, v in values.items() if k != "ratios"}
        if "modes" in values:
            # The whole effective map is stored, defaults included, so a plan
            # built at a prompt does not need the modes repeated as flags.
            self._write_table_setting("mode", {
                **DEFAULT_SETTINGS["modes"],
                **self._read_table_setting("mode"),
                **self._checked_modes(values["modes"]),
            })
            values = {k: v for k, v in values.items() if k != "modes"}
        merged = {**self.read_settings(), **values}
        # Modes and ratios are kept in the decisions file, so they are not
        # written here as well. Two copies of the same answer only drift.
        self.path("settings").write_text(
            json.dumps(
                {k: v for k, v in merged.items() if k not in ("modes", "ratios")},
                indent=2,
            ),
            encoding="utf-8",
        )
        return merged

    def _read_table_setting(self, key: str) -> dict[str, Any]:
        """One decision per table, from the file the command line shares."""
        return {
            name: setting[key]
            for name, setting in (self.read_decisions().get("tables") or {}).items()
            if isinstance(setting, dict) and setting.get(key)
        }

    def _write_table_setting(self, key: str, values: dict[str, Any]) -> None:
        """Replace every table's setting of one kind, leaving the others alone.

        Modes and ratios share a home, so clearing one must not clear the
        other, and a table left with neither should not linger as an empty
        entry.
        """
        document = self.read_decisions()
        tables = document.setdefault("tables", {})
        for name in list(tables):
            if isinstance(tables[name], dict):
                tables[name].pop(key, None)
                if not tables[name]:
                    del tables[name]
        for name, setting in values.items():
            tables.setdefault(name, {})[key] = setting
        save_decisions(document, self.path("decisions"))

    @staticmethod
    def _checked_modes(modes: Any) -> dict[str, str]:
        """Validate what a run should do with each table before storing it."""
        cleaned: dict[str, str] = {}
        for table, mode in (modes or {}).items():
            if mode is None:
                continue
            if mode not in TABLE_MODES:
                raise ServiceError(
                    f"{table}: there is no mode called {mode}. It has to be "
                    + ", ".join(TABLE_MODES) + "."
                )
            cleaned[table] = mode
        return cleaned

    @staticmethod
    def _checked_ratios(ratios: Any) -> dict[str, Any]:
        """Validate every edited ratio before any of them is stored.

        A ratio that will not build is caught here rather than when the plan is
        next built, where the message would arrive far from the field that
        caused it.
        """
        from sdg.plan.editing import check_ratio, coerce_ratio

        cleaned: dict[str, Any] = {}
        for table, setting in (ratios or {}).items():
            if not setting or setting.get("kind") in (None, "measured"):
                continue  # dropping it is how you go back to what was measured
            shape = setting["kind"]
            params = coerce_ratio(
                shape, {k: v for k, v in setting.items() if k != "kind"}
            )
            problems = check_ratio(shape, params)
            if problems:
                raise ServiceError(f"{table}: " + " ".join(problems))
            cleaned[table] = {"kind": shape, **params}
        return cleaned

    def _column(self, table: str, column: str) -> Any:
        """The physical column an edit is about, when the schema has been read."""
        if not self.exists("model"):
            return None
        try:
            model = self.require_model()
        except ServiceError:
            return None
        found = next((t for t in model.tables if t.name == table), None)
        if found is None:
            return None
        return next((c for c in found.columns if c.name == column), None)

    def read_decisions(self) -> dict[str, Any]:
        return load_decisions(self.path("decisions"))

    def write_decisions(self, document: dict[str, Any]) -> dict[str, Any]:
        """Replace the decisions file wholesale, for a caller that rewrote it."""
        save_decisions(document, self.path("decisions"))
        return document

    def record_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Approve, change, or reopen one column. Keyed on table and column.

        Any column can be changed, not only the ones the tool was unsure about.
        A rule it worked out for itself is still a proposal, and a person who
        knows the schema better is allowed to say so.
        """
        document = self.read_decisions()
        columns = document.setdefault("columns", [])
        key = (decision["table"], decision["column"])

        if decision.get("reset"):
            document["columns"] = [
                c for c in columns if (c.get("table"), c.get("column")) != key
            ]
            save_decisions(document, self.path("decisions"))
            return {"table": key[0], "column": key[1], "reset": True}

        generator = decision.get("generator")
        if generator:
            params = coerce(generator, decision.get("params") or {})
            problems = check(generator, params)
            problems += check_fits(generator, params, self._column(
                decision["table"], decision["column"],
            ))
            if problems:
                raise ServiceError(" ".join(problems))
            decision = {**decision, "params": params}

        existing = next(
            (c for c in columns if (c.get("table"), c.get("column")) == key), None
        )
        if existing is None:
            existing = {"table": decision["table"], "column": decision["column"]}
            columns.append(existing)

        for name in ("approved", "element", "generator", "params"):
            if name in decision:
                existing[name] = decision[name]

        save_decisions(document, self.path("decisions"))
        return existing

    # -- pipeline --------------------------------------------------------

    def refuse_protected(self, doing: str) -> None:
        """Stop before reading or writing the database worked on, if it is
        production. Whatever was chosen or configured: the default target is
        the source, and at a customer the source is production."""
        _refuse_protected(self.registry.database_for("target"), doing)

    def connect(self) -> dict[str, Any]:
        import json

        self.refuse_protected("read")

        checks = [self.registry.check(name) for name in ("source", "ref")]
        payload = {
            "connections": [self.registry.ref(n).as_dict() for n in ("source", "ref")],
            "checks": checks,
        }
        self.path("connections").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def discover(self) -> dict[str, Any]:
        self.refuse_protected("read")
        started = time.perf_counter()
        result = run_discovery(self.registry, "source")
        result.model.save(self.path("model"))
        self._cache.clear()
        return {
            **result.summary(),
            "inferred": [
                {
                    "childTable": key.child_table,
                    "childColumn": key.child_column,
                    "parentTable": key.parent_table,
                    "parentColumn": key.parent_column,
                    "coverage": key.coverage,
                    "distinctValues": key.distinct_values,
                    "evidence": key.evidence(),
                }
                for key in result.inferred
            ],
            "seconds": round(time.perf_counter() - started, 2),
        }

    def profile(self) -> dict[str, Any]:
        self.refuse_protected("read")
        started = time.perf_counter()
        # The workspace's catalog: the one generation follows says which
        # columns are personal, and so whose values the profile withholds.
        report = run_profiling(self.registry, self.require_model(), catalog=self.catalog)
        report.save(self.path("profile"))
        self._cache.clear()
        return {
            "columns": len(report.columns),
            "formatsFitted": sum(1 for c in report.columns if c.fitted_format),
            "dependencies": len(report.dependencies),
            "orderings": len(report.orderings),
            "ratios": len(report.ratios),
            "skipped": [s.model_dump() for s in report.skipped],
            "seconds": round(time.perf_counter() - started, 2),
        }

    def _landscape_path(self, settings: dict[str, Any] | None = None) -> Path | None:
        """The pattern store's description on file: the one named in settings,
        else the one for the source in `.env`, else whichever there is."""
        settings = settings if settings is not None else self.read_settings()
        named = settings.get("landscape")
        for name in [named, self.settings.source_db] if named else [self.settings.source_db]:
            found = self.work / f"landscape-{name}.json"
            if found.exists():
                return found
        if named:
            return None
        return next(iter(sorted(self.work.glob("landscape-*.json"))), None)

    def landscape(self):
        """What the pattern store says data looks like, if it has been described."""
        found = self._landscape_path()
        if found is None:
            return None
        from sdg.landscape import load

        return load(found)

    def map(self) -> dict[str, Any]:
        started = time.perf_counter()
        model = self.require_model()
        store = map_columns(model, self.catalog, self.report)

        # An empty table cannot show a key nobody declared. Its columns' elements
        # and the pattern store can: a claim's MEMBER_KEY is the member table's
        # key, and the store holds claims per member.
        added: list[str] = []
        shape = self.landscape()
        if shape is not None and any(not t.row_count for t in model.tables):
            from sdg.discovery.classify import classify
            from sdg.landscape.project import landscape_keys

            added = landscape_keys(model, store, shape)
            if added:
                classify(model)
                model.save(self.path("model"))
                store = map_columns(model, self.catalog, self.report)

        store.save(self.path("mappings"))
        self._cache.clear()
        return {**store.statistics(), "keysFromLandscape": added,
                "seconds": round(time.perf_counter() - started, 2)}

    def proposals(self) -> list[Proposal]:
        """Proposals are recomputed rather than cached: they depend on the data.

        Which data is the point. For a database with rows it is this one. For a
        target waiting to be filled there is none, and the proposals come from
        what the landscape says a column holding each element looks like --
        otherwise every column of an empty target is proposed as `skip`, which
        is both correct about the evidence and useless.
        """
        model = self.require_model()
        sums = list(self._measured_sums(model))
        # A total in an empty table cannot show it is a sum; the pattern store
        # can, where its elements match.
        landscape = self.landscape()
        if landscape is not None and self.mappings is not None:
            from sdg.landscape.project import landscape_sums

            shown = {(s.parent_table, s.parent_column) for s in sums}
            sums += [s for s in landscape_sums(model, self.mappings, landscape)
                     if (s.parent_table, s.parent_column) not in shown]
        shape = self._shape_for(self.read_settings(), model, self.mappings)
        return propose(model, shape, sums, self.mappings, self.catalog)

    def _measured_sums(self, model) -> list:
        """Which totals are their children's sums: measured on the source once
        per read of its structure, not on every page that shows a rule. It is
        a query for each link, and a database of thousands of tables has
        thousands of links. What was measured is kept beside the model, so the
        tool started again does not measure it again either."""
        import json
        from dataclasses import asdict

        stamp = self.path("model").stat().st_mtime_ns if self.exists("model") else None
        held = getattr(self, "_sums", None)
        if held is not None and held[0] == (self.work, stamp):
            return held[1]
        sums = None
        if stamp is not None and self.exists("sums"):
            try:
                saved = json.loads(self.path("sums").read_text(encoding="utf-8"))
                if saved.get("model") == stamp:
                    sums = [SumExpression(**s) for s in saved["sums"]]
            except (ValueError, TypeError, KeyError):
                sums = None
        if sums is None:
            sums = detect_sum_expressions(self.registry, model)
            if stamp is not None:
                self.path("sums").write_text(
                    json.dumps({"model": stamp, "sums": [asdict(s) for s in sums]}),
                    encoding="utf-8",
                )
        self._sums = ((self.work, stamp), sums)
        return sums

    def queued_count(self) -> int | None:
        """How many columns wait for a decision, counted from the rules last
        proposed rather than by proposing them again: the count is on every
        page, and proposing reads the source. None when the rules are older
        than the structure read since."""
        import json

        if not self.exists("proposals") or not self.exists("model"):
            return None
        if self.path("proposals").stat().st_mtime_ns < self.path("model").stat().st_mtime_ns:
            return None
        try:
            saved = json.loads(self.path("proposals").read_text(encoding="utf-8"))
            proposals = [Proposal(**p) for p in saved]
        except (ValueError, TypeError):
            return None
        return len(build_queue(self.require_model(), self.require_mappings(), proposals))

    def review(self) -> dict[str, Any]:
        import json

        started = time.perf_counter()
        proposals = self.proposals()
        self.path("proposals").write_text(
            json.dumps([p.as_dict() for p in proposals], indent=2, default=str),
            encoding="utf-8",
        )
        queue = build_queue(self.require_model(), self.require_mappings(), proposals)
        by_confidence: dict[str, int] = {}
        for proposal in proposals:
            by_confidence[proposal.confidence] = by_confidence.get(proposal.confidence, 0) + 1
        return {
            "proposals": len(proposals),
            "byConfidence": by_confidence,
            "queued": len(queue),
            "seconds": round(time.perf_counter() - started, 2),
        }

    # -- plan ------------------------------------------------------------

    def _shape_for(self, settings: dict[str, Any], model, mappings):
        """Where the measurements behind a plan come from.

        A profile of this database, when there is one. A target may have none
        -- it is a schema waiting to be filled, and reading its data is both
        impossible and beside the point. In that case the shape comes from the
        landscape: what the store says a column holding this element looks
        like, and how many of these there are per one of those.

        The landscape is used only to fill what profiling did not reach, never
        to overrule it. A measurement of this database beats a measurement of
        something else carrying the same element, every time.
        """
        measured = self.report
        if mappings is None:
            return measured
        landscape_path = self._landscape_path(settings)
        if landscape_path is None:
            return measured

        from sdg.landscape import load
        from sdg.landscape.project import project

        projected = project(
            load(landscape_path), model, mappings,
            subjects=dict(settings.get("subjects") or {}),
        )
        if measured is None:
            return projected

        # Columns the landscape could not be used for, because their own type
        # cannot hold what it holds, are said rather than silently dropped.
        measured = measured.model_copy(deep=True)
        measured.skipped.extend(projected.skipped)
        have = {(c.table, c.column) for c in measured.columns if c.row_count}
        measured.columns = [c for c in measured.columns if c.row_count]
        measured.columns.extend(
            c for c in projected.columns if (c.table, c.column) not in have
        )
        measured.ratios = [r for r in measured.ratios if r.parents]
        rated = {(r.child_table, r.parent_table) for r in measured.ratios}
        measured.ratios.extend(
            r for r in projected.ratios if (r.child_table, r.parent_table) not in rated
        )
        return measured

    def build(self) -> tuple[Plan, Any, dict[str, Any]]:
        settings = self.read_settings()
        model = self.require_model()
        mappings = self.require_mappings()
        proposals = self.proposals()

        inputs = BuildInputs(
            model=model,
            catalog=self.catalog,
            mappings=mappings,
            proposals=proposals,
            report=self._shape_for(settings, model, mappings),
            decisions=self.read_decisions(),
            modes=dict(settings.get("modes") or {}),
            root_counts={k: int(v) for k, v in (settings.get("rootCounts") or {}).items()},
            driving_parents=dict(settings.get("drivingParents") or {}),
            seed=settings.get("seed") or "demo-seed-001",
            name=settings.get("planName") or held("pack:plan_name"),
        )
        inputs.reference_sets = default_reference_sets(self.registry, inputs)
        inputs.combinations = self.combinations()
        plan, graph = build_plan(inputs)
        plan.save(self.path("plan"))
        self._cache.clear()
        return plan, graph, {
            "references": {
                name: {
                    "table": reference.table,
                    "size": reference.size,
                    "available": reference.available,
                    "strategy": reference.strategy,
                    "filter": reference.filter,
                    "describe": reference.describe(),
                }
                for name, reference in inputs.reference_sets.items()
            }
        }

    def run_for_export(self, limit: int | None = None, plan: Plan | None = None):
        """Generated rows for an export, kept between exports of the same run.

        Re-running the plan would give the same rows -- the seed decides them --
        but it costs seconds, and a user exporting an 834 and then an 837 of the
        same population should not pay for it twice. The key is the whole plan
        plus the limit, so a replanned or reseeded run is a different dataset
        and is generated afresh. `plan` is the one a file was first written
        from, when it is written again.
        """
        import hashlib
        import json

        from sdg.engine.execute import refuse_never_generated, run_plan

        plan = plan or self.require_plan()
        document = json.dumps(plan.to_dict(), sort_keys=True, default=str)
        key = (hashlib.sha256(document.encode("utf-8")).hexdigest(), limit)
        if self._export_run and self._export_run[0] == key:
            return self._export_run[1]
        refuse_never_generated(plan)
        sets, references = self.reference_sets_with_sources(plan)
        result = run_plan(plan, limit=limit, reference_sets=sets)
        self._export_run = (key, result, self.reused_keys(references))
        return result

    def export_reused_keys(self) -> dict[str, Any]:
        """The keys the last export's run reused, by fingerprint, without the keys."""
        return self._export_run[2] if self._export_run else {}

    @staticmethod
    def reused_keys(references: dict[str, Any]) -> dict[str, Any]:
        from sdg.api.load_requests import reference_fingerprint

        return {name: reference_fingerprint(found)
                for name, found in sorted(references.items())}

    def coverage_plans(self) -> dict[str, dict[str, Any]]:
        """Plan id to the descriptive fields an enrollment transaction names.

        One row per plan, so this is a small read and is held for the process.
        """
        if self._coverage_plans is not None:
            return self._coverage_plans
        found: dict[str, dict[str, Any]] = {}
        try:
            rows = self.registry.query(
                "source",
                "SELECT p.CSPI_ID, p.CSPD_CAT, d.PLDS_DESC "
                "FROM dbo.CMC_CSPI_CS_PLAN AS p "
                "LEFT JOIN dbo.CMC_PLDS_PLAN_DESC AS d ON d.CSPI_ID = p.CSPI_ID",
            )
            for row in rows:
                found[str(row["CSPI_ID"]).strip()] = {
                    "CSPD_CAT": (row["CSPD_CAT"] or "").strip(),
                    "PLDS_DESC": row["PLDS_DESC"],
                }
        except Exception:
            # An export can still be produced without the plan description; it
            # is a label on the coverage, not the coverage itself. Not held:
            # a passing failure must not drop the labels until a restart.
            log.warning("Plan descriptions could not be read; the export "
                        "goes without them.", exc_info=True)
            return {}
        self._coverage_plans = found
        return found

    def reference_sets_for_run(self):
        """Keys for every table the plan reuses, ready for the engine."""
        return self.reference_sets_with_sources(self.require_plan())[0]

    def reference_sets_with_sources(self, plan: Plan):
        """The engine's keys for every table `plan` reuses, and the sets they
        were read as -- what a load pointed at, for its record."""
        from sdg.engine.execute import ReferenceSets

        sets = ReferenceSets()
        loaded: dict[str, Any] = {}
        wanted = attributes_filtered_on(plan)
        bindings = {
            table.name: {
                "connection": table.source.connection,
                "table": table.source.table,
                "schema": table.source.schema_name,
                "key_column": table.source.key_columns[0],
                "filter": table.source.filter,
                "load": table.source.load or "full",
                "sample_size": table.source.sample_size,
                # Only what something filters on. Loading every column of a
                # reference set to use one of them is the kind of cost that
                # does not show up until the set is large.
                "attributes": sorted(wanted.get(table.name, set())),
            }
            for table in plan.tables
            if table.mode == "reuse" and table.source
        }
        if bindings:
            loaded = load_for_plan(self.registry, bindings, seed=plan.meta.seed)
            for name, reference in loaded.items():
                sets.add(name, reference.keys, reference.attributes)
        return sets, loaded

    # -- run -------------------------------------------------------------

    def dry_run(self, rows: int = 5) -> dict[str, Any]:
        from sdg.engine.dryrun import dry_run
        from sdg.engine.execute import refuse_never_generated

        self.refuse_protected("read")
        plan = self.require_plan()
        refuse_never_generated(plan)
        preview = dry_run(plan, rows=rows, reference_sets=self.reference_sets_for_run())
        return {
            "tables": [
                {
                    "name": table.name,
                    "columns": [f.name for f in table.fields if f.generator != "skip"],
                    "rows": [
                        {k: _plain(v) for k, v in row.items()}
                        for row in preview.result.rows(table.name)[:rows]
                    ],
                    "generated": len(preview.result.rows(table.name)),
                }
                for table in plan.tables_in_order()
            ],
            "seconds": round(preview.result.seconds, 2) if preview.result else 0,
        }

    def generate(self, on_written=None, *, seed: str | None = None,
                 disable_triggers: bool = False) -> dict[str, Any]:
        """Generate the plan into the target.

        `on_written(plan, ran, result, report, references, partial)` is told
        once rows are in the target -- the load's record for audit -- and what
        it returns is given back as `request`. `seed` replaces the plan's for
        this run only; the screens and the command line load the same way."""
        from sdg.config import DATABASE_GENERATION_OFF, database_generation
        from sdg.engine.execute import refuse_never_generated, run_plan

        # Said before any rows are made, rather than after minutes of making
        # them; the writer refuses as well.
        if not database_generation():
            raise ServiceError(DATABASE_GENERATION_OFF)
        self.refuse_protected("write into")
        # This run's record is merged with the last one's until cleanup, and
        # cleanup deletes every key in it from one database: a run into
        # another database cannot share it.
        if self.exists("manifest"):
            try:
                earlier = load_manifest(self.path("manifest"))
            except (ValueError, OSError) as e:
                raise ServiceError(
                    f"The last run's record ({self.path('manifest')}) cannot be read: {e}. "
                    "Nothing was generated.") from e
            if not isinstance(earlier, dict):
                raise ServiceError(
                    f"The last run's record ({self.path('manifest')}) is not a run record. "
                    "Nothing was generated.")
            try:
                check_cleanup_target(self.registry, earlier, "target")
            except WrongDatabase as e:
                written = (earlier.get("run") or {}).get("database")
                if not written:
                    raise ServiceError(
                        f"The last run's record ({self.path('manifest')}) does not say which "
                        "database its rows are in, so this run cannot be added to it. Find "
                        "that run's rows from its record, remove them, and move the record "
                        "aside. Nothing was generated.") from e
                server = (earlier.get("run") or {}).get("server")
                if written.strip().upper() == self.registry.database_for("target").strip().upper():
                    raise ServiceError(
                        f"The last run's rows in {written} on {server} have not been removed, "
                        "and one record cannot cover rows on two servers. Connect to "
                        f"{server} and run cleanup first. Nothing was generated.") from e
                raise ServiceError(
                    f"The last run's rows in {written} have not been removed, and one record "
                    f"cannot cover rows in two databases. Choose {written} as the target and "
                    "run cleanup first. Nothing was generated.") from e
        plan = self.require_plan()
        if seed:
            plan = plan.model_copy(deep=True)
            plan.meta.seed = seed
        stoppers = blocking(plan.warnings)
        if stoppers:
            raise ServiceError(
                "The plan has blocking warnings. Clear them before generating: "
                + "; ".join(w.message for w in stoppers[:3])
            )

        # Where the keys start is read from the target now, not remembered
        # from when it was profiled. The rows the last run wrote are in the
        # table and the plan does not know about them.
        from sdg.engine.starts import resolve, summarize

        planned = plan
        plan, moved = resolve(self.registry, plan, connection="target")

        sets, references = self.reference_sets_with_sources(plan)
        refuse_never_generated(plan)
        result = run_plan(plan, reference_sets=sets)
        try:
            report = write(
                self.registry, plan, result, self.require_model(), connection="target",
                disable_triggers=disable_triggers,
            )
        except PartialWrite as partial:
            # Whole tables were committed before this failed. Their manifest is
            # written anyway, so cleanup can still find those rows -- merged
            # with the last run's, whose rows are still there too.
            stopped = build_manifest(
                plan, partial.report,
                connection="target",
                database=self.registry.database_for("target"),
                server=self.registry.settings_for("target").endpoint,
            )
            if self.exists("manifest"):
                stopped = merge_manifests(load_manifest(self.path("manifest")), stopped)
            save_manifest(stopped, self.path("manifest"))
            self._cache.clear()
            if on_written is not None:
                on_written(planned, plan, result, partial.report, references, True)
            written = ", ".join(
                f"{t.table} {t.rows_written:,} rows" for t in partial.report.tables
            )
            raise ServiceError(
                f"The load stopped partway: {partial.cause} "
                f"Already in the database: {written}. Run cleanup to remove them."
            ) from partial
        manifest = build_manifest(
            plan, report,
            connection="target",
            database=self.registry.database_for("target"),
            server=self.registry.settings_for("target").endpoint,
        )
        # A run that has not been cleaned up yet still has rows in the target,
        # and this manifest is the only thing that can find them. Writing over
        # it would strand them.
        if self.exists("manifest"):
            manifest = merge_manifests(
                load_manifest(self.path("manifest")), manifest
            )
        save_manifest(manifest, self.path("manifest"))
        self._cache.clear()
        kept = (on_written(planned, plan, result, report, references, False)
                if on_written is not None else None)
        return {
            "request": kept,
            "tables": [t.as_record() for t in report.tables],
            "rowsWritten": report.total_rows(),
            "seconds": round(report.seconds, 2),
            "triggersFired": trigger_side_effects(manifest),
            # Said out loud. A key starting somewhere other than where the plan
            # says is the right thing to do and the wrong thing to do silently.
            "keysAdvanced": summarize(moved),
        }

    def validate(self) -> dict[str, Any]:
        self.refuse_protected("read")
        manifest = self.manifest
        if manifest is None:
            raise ServiceError("Generate some rows first: there is nothing to check.")
        report = validate_run(
            self.registry, self.require_plan(), self.require_model(), manifest,
            connection="target", reference_sets=self.reference_sets_for_run().keys,
        )
        summary = report.summary()
        manifest["validation"] = summary
        save_manifest(manifest, self.path("manifest"))
        self._cache.clear()
        return {
            **summary,
            "results": [c.as_record() for c in report.checks],
        }

    def cleanup(self) -> dict[str, Any]:
        self.refuse_protected("delete from")
        manifest = self.manifest
        if manifest is None:
            raise ServiceError("There is no run to remove.")
        try:
            deleted = run_cleanup(self.registry, manifest, connection="target")
        except WrongDatabase as e:
            raise ServiceError(str(e)) from e
        path = self.path("manifest")
        path.replace(path.with_suffix(".cleaned.json"))
        self._cache.clear()
        return {
            "deleted": deleted,
            "total": sum(deleted.values()),
            "triggersFired": trigger_side_effects(manifest),
        }


def _plain(value: Any) -> Any:
    """A value the browser can hold without losing its meaning."""
    from datetime import date, datetime
    from decimal import Decimal

    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _mssql_from(config: dict[str, Any], settings) -> "MssqlSettings":
    """One configured connection, as the thing the registry speaks."""
    import dataclasses

    return dataclasses.replace(
        settings.mssql,
        server=(config.get("server") or "").strip() or settings.mssql.server,
        port=int(config.get("port") or settings.mssql.port),
        user=(config.get("user") or "").strip() or settings.mssql.user,
        password=config.get("password") or "",
        driver=(config.get("driver") or "").strip() or settings.mssql.driver,
        trust_cert=bool(config.get("trustCert", True)),
        trusted=bool(config.get("trusted")),
    )


def _protected(stored: dict[str, Any]) -> dict[str, Any]:
    """A saved connection as it is written: on Windows the password encrypted
    for this account on this machine, never in plain text."""
    if not stored.get("password") or not dpapi.available():
        return stored
    written = {k: v for k, v in stored.items() if k != "password"}
    written["password_protected"] = dpapi.protect(stored["password"])
    return written


def _write_private(path: Path, text: str) -> None:
    """Write `text` readable by its owner only, where the platform allows.

    Written to a new file beside it first, limited to its owner (0600 on
    POSIX; on Windows inherited access removed and only the signed-in account
    granted), then moved over the old one. A failure leaves the old file as it
    was, and the password is never written into a file anyone else can read.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            if os.name == "nt":
                _owner_only_on_windows(temp)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _owner_only_on_windows(path: Path) -> None:
    """Remove the file's inherited access and grant the signed-in account,
    named by its SID, full control only. Refuses if that fails."""
    import subprocess

    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, check=False)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip().strip('"') if who.returncode == 0 else ""
    if not sid.startswith("S-1-"):
        raise ServiceError(
            f"Could not tell which Windows account is signed in, so the password was not "
            f"saved: {(who.stderr or who.stdout).strip()}")
    done = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ServiceError(
            f"Could not limit {path.name} to the signed-in account, so the password was "
            f"not saved: {(done.stderr or done.stdout).strip()}")
