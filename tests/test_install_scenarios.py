"""The scripts a person runs on a machine, run the way they run them.

Each scenario drives the real `scripts\\*.cmd` files in a subprocess, on a
machine of its own: databases named SDMT_* and a working directory in a temp
folder, all named through the environment exactly as `.env` would name them.
Nothing here reads or writes FACETS_QA, MDM_REF, SDM_DENORM or work/.

In order, because each is what a person does next:

1. a bare machine installs with the scripts alone;
2. running every script again changes nothing it should not;
3. a machine set up from the previous version upgrades in one command, and
   keeps what people made on it -- saved patterns, layouts, rule edits;
4. while the interface works on another onboarded database, every script
   that writes into work/ refuses and leaves that database's files exactly as
   they were; back on the source, they run again.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyodbc
import pytest

from sdg.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS = ROOT / "sdg" / "packs" / "health" / "extract"

SOURCE, REF, STORE, OTHER = "SDMT_FACETS", "SDMT_REF", "SDMT_DENORM", "SDMT_OTHER"
OURS = (SOURCE, REF, STORE, OTHER)
FACETS_TABLES = ("CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE")

pytestmark = [pytest.mark.db, pytest.mark.slow,
              pytest.mark.skipif(sys.platform != "win32", reason="the scripts are .cmd files")]


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------

def _master():
    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _drop(name: str) -> None:
    assert name.startswith("SDMT_"), f"refusing to drop {name}"
    with _master() as master:
        if master.execute("SELECT DB_ID(?)", name).fetchone()[0]:
            master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{name}]")


class Machine:
    def __init__(self, work: Path):
        self.work = work
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SDG_DB_")}
        self.env.update({
            "SDG_DB_SOURCE": SOURCE, "SDG_DB_REF": REF, "SDG_DB_DENORM": STORE,
            "SDG_DB_TARGET": SOURCE, "SDG_WORK_DIR": str(work),
            "PYTHONIOENCODING": "utf-8", "COLUMNS": "200",
        })

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["cmd", "/c", f"scripts\\{script}", *args], cwd=ROOT, env=self.env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1200,
        )
        result.output = result.stdout + result.stderr
        return result

    def python(self, code: str) -> str:
        """Run code the way the interface would, on this machine."""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=self.env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def query(self, database: str, sql: str, *params, **_):
        conn = pyodbc.connect(get_settings().mssql.connection_string(database))
        try:
            return [tuple(row) for row in conn.execute(sql, *params).fetchall()]
        finally:
            conn.close()

    def execute(self, database: str, sql: str, *params) -> None:
        conn = pyodbc.connect(get_settings().mssql.connection_string(database), autocommit=True)
        try:
            conn.execute(sql, *params)
        finally:
            conn.close()

    def facets_counts(self, database: str = SOURCE) -> dict[str, int]:
        return {t: self.query(database, f"SELECT COUNT(*) FROM dbo.{t}")[0][0]
                for t in FACETS_TABLES}

    def store_counts(self) -> dict[str, int]:
        tables = ("member_denorm", "claims_denorm", "patterns", "layouts", "generation_rules")
        # sdm.layouts is made by the analyzer the first time a layout is kept.
        return {t: (self.query(STORE, f"SELECT COUNT(*) FROM sdm.{t}")[0][0]
                    if self.query(STORE, f"SELECT OBJECT_ID('sdm.{t}')")[0][0] else -1)
                for t in tables}

    def snapshot(self) -> dict[str, str]:
        """Every working file that belongs to one database, by content."""
        out = {}
        for path in sorted(self.work.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.work).as_posix()
            if rel.startswith("landscape-") or rel in ("server.log",) or "cache" in rel:
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out


@pytest.fixture(scope="module")
def machine():
    for name in OURS:
        _drop(name)
    work = Path(tempfile.mkdtemp(prefix="sdm-machine-"))
    # Every source is read through its own extract definition; this one reads
    # exactly what the fixture's does.
    definition = DEFINITIONS / f"{SOURCE}.sql"
    shutil.copy2(DEFINITIONS / "FACETS_QA.sql", definition)
    try:
        yield Machine(work)
    finally:
        definition.unlink(missing_ok=True)
        for name in OURS:
            _drop(name)
        shutil.rmtree(work, ignore_errors=True)


def _ok(result) -> None:
    assert result.returncode == 0, result.output[-4000:]


# ---------------------------------------------------------------------------
# 1. A bare machine
# ---------------------------------------------------------------------------

def test_1_a_bare_machine_installs_with_the_scripts_alone(machine):
    check = machine.run("bootstrap.cmd", "--check")
    assert check.returncode == 1 and "MISSING" in check.output, check.output

    _ok(machine.run("bootstrap.cmd"))
    _ok(machine.run("bootstrap.cmd", "--check"))
    _ok(machine.run("seed.cmd", "--small"))
    seeded = machine.facets_counts()
    assert all(seeded.values()), seeded

    first = machine.run("first-run.cmd")
    _ok(first)
    assert "post-run validation" in first.output and "First run complete" in first.output

    assert machine.facets_counts() == seeded, "first-run left generated rows in the source"
    members = machine.query(STORE, "SELECT COUNT(*), COUNT(SBSB_ID), COUNT(MEME_SFX) "
                                   "FROM sdm.member_denorm")[0]
    assert members[0] == seeded["CMC_MEME_MEMBER"] and members[1] == members[2] == members[0], (
        "every member in the store carries its subscriber ID and suffix")
    assert machine.store_counts()["generation_rules"] > 0
    for name in ("physical-model.json", "profile.json", "mappings.json", "plan.json"):
        assert (machine.work / name).exists(), name
    assert (machine.work / f"landscape-{SOURCE}.json").exists()
    decisions = json.loads((machine.work / "review-decisions.json").read_text(encoding="utf-8"))
    assert any(c["column"] == "CDML_SVC_CD" and c["element"] == "PROCEDURE_CODE"
               for c in decisions["columns"]), "the project's decisions were not applied"


# ---------------------------------------------------------------------------
# 2. Everything again
# ---------------------------------------------------------------------------

def test_2_running_every_script_again_changes_nothing_it_should_not(machine):
    facets, store = machine.facets_counts(), machine.store_counts()
    _ok(machine.run("bootstrap.cmd"))
    _ok(machine.run("first-run.cmd"))
    _ok(machine.run("first-run.cmd"))
    assert machine.facets_counts() == facets
    assert machine.store_counts() == store


# ---------------------------------------------------------------------------
# 3. Upgrading from the previous version
# ---------------------------------------------------------------------------

def _as_previous_version(machine) -> None:
    """Put the machine back to what the previous zip left: a store whose
    extract tables carry a source column, a demo source with no member suffix,
    and no record of whose files are in work/."""
    machine.execute(STORE, "ALTER TABLE sdm.member_denorm ADD source varchar(64) NULL")
    machine.execute(STORE, "ALTER TABLE sdm.claims_denorm ADD source varchar(64) NULL")
    machine.execute(SOURCE, "DROP VIEW sdm.member_denorm")
    machine.execute(SOURCE, "ALTER TABLE dbo.CMC_MEME_MEMBER DROP COLUMN MEME_SFX")
    (machine.work / "working-database.txt").unlink(missing_ok=True)


def test_3_a_machine_on_the_previous_version_upgrades_and_keeps_what_people_made(machine):
    _as_previous_version(machine)

    # What people made there, which nothing can recreate.
    machine.execute(STORE, "INSERT INTO sdm.patterns (pattern_key, contextual_name, cluster_id, "
                           "member_count, saved_by) VALUES ('saved|machine-test', 'Kept', 0, 3, 'user')")
    machine.python("from sdg.analyzer.services.layout_service import ensure_table; ensure_table()")
    machine.execute(STORE, "INSERT INTO sdm.layouts (name, kind, field_spec) "
                           "VALUES ('Kept layout', 'delimited', '[]')")
    machine.execute(STORE, "UPDATE sdm.generation_rules SET field_label = 'Edited by hand' "
                           "WHERE id = (SELECT MIN(id) FROM sdm.generation_rules)")
    edited = machine.query(STORE, "SELECT MIN(id) FROM sdm.generation_rules")[0][0]
    facets = machine.facets_counts()

    upgrade = machine.run("upgrade.cmd")
    _ok(upgrade)
    assert "Upgrade complete" in upgrade.output

    # The store's tables took the customer's shape and were filled again; the
    # demo source was rebuilt, at its own size, with the member suffix.
    assert "rebuilding at scale" in upgrade.output
    assert not machine.query(STORE, "SELECT COL_LENGTH('sdm.member_denorm', 'source')")[0][0]
    members = machine.query(STORE, "SELECT COUNT(*), COUNT(MEME_SFX) FROM sdm.member_denorm")[0]
    assert members[0] and members[1] == members[0], "the upgrade did not fill the store"
    assert machine.query(SOURCE, "SELECT COL_LENGTH('sdm.member_denorm', 'MEME_SFX')")[0][0], (
        "the source's extract view was not brought up to date")
    assert machine.query(STORE, "SELECT contextual_name FROM sdm.patterns "
                                "WHERE pattern_key = 'saved|machine-test'") == [("Kept",)]
    assert machine.query(STORE, "SELECT COUNT(*) FROM sdm.layouts WHERE name = 'Kept layout'")[0][0] == 1
    assert machine.query(STORE, "SELECT field_label FROM sdm.generation_rules WHERE id = ?",
                         edited) == [("Edited by hand",)], "an edited rule was overwritten"
    assert machine.facets_counts() == facets, "the upgrade changed the source's rows"

    _ok(machine.run("upgrade.cmd"))
    assert machine.query(STORE, "SELECT COUNT(*) FROM sdm.patterns "
                                "WHERE pattern_key = 'saved|machine-test'")[0][0] == 1


# ---------------------------------------------------------------------------
# 4. Scripts while the interface works on another database
# ---------------------------------------------------------------------------

def _choose(machine, database: str) -> None:
    machine.python(
        "from pathlib import Path; from sdg.api.service import Service; "
        f"s = Service(work_dir=Path(r'{machine.work}')); s.set_target({database!r})"
    )


def _read_and_plan(machine) -> None:
    machine.python(
        "from pathlib import Path; from sdg.api.service import Service; "
        f"s = Service(work_dir=Path(r'{machine.work}')); "
        "s.connect(); s.discover(); s.profile(); s.map(); s.review(); s.build()"
    )


def test_4_scripts_refuse_while_another_database_is_worked_on(machine):
    from tests import facets_copies

    facets_copies.build(OTHER)
    _choose(machine, OTHER)
    _read_and_plan(machine)
    assert (machine.work / "working-database.txt").read_text(encoding="utf-8").endswith(OTHER)
    before = machine.snapshot()
    assert "plan.json" in before and f"databases/" in " ".join(before), before
    store = machine.store_counts()

    for script in ("first-run.cmd", "upgrade.cmd"):
        refused = machine.run(script)
        assert refused.returncode != 0, f"{script} ran while {OTHER} was worked on"
        assert "belongs to" in refused.output and OTHER in refused.output, refused.output[-2000:]
        assert machine.snapshot() == before, f"{script} changed {OTHER}'s files"

    demo = machine.run("demo.cmd") if (ROOT / "scripts" / "demo.cmd").exists() else None
    if demo is not None:
        assert demo.returncode != 0 and machine.snapshot() == before

    decisions = subprocess.run(
        [sys.executable, "seed/decisions.py"], cwd=ROOT, env=machine.env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert decisions.returncode == 0 and "left alone" in decisions.stdout, decisions.stdout
    assert machine.snapshot() == before, "the decisions landed in another database's review"
    assert machine.store_counts() == store

    # Back on the source, everything runs; and the other database's files are
    # waiting, exactly as they were, when it is chosen again.
    shelved = {k: v for k, v in before.items() if "/" not in k}
    _choose(machine, SOURCE)
    _ok(machine.run("first-run.cmd"))
    _choose(machine, OTHER)
    after = {k: v for k, v in machine.snapshot().items() if "/" not in k}
    for name, digest in shelved.items():
        if name in ("ui-settings.json", "working-database.txt"):
            continue
        assert after.get(name) == digest, f"{OTHER}'s {name} did not come back as it was"


# ---------------------------------------------------------------------------
# 5. The demo scripts on a customer machine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("setting", [{"SDG_ENV": "customer"},
                                     {"SDG_DENORM_SERVER": r"customer-sql.example\SDM"}])
def test_5_demo_scripts_refuse_on_a_customer_machine(machine, setting):
    """At the customer an upgrade or first-run would re-extract the demo source
    over their store; every demo script stops before touching anything."""
    facets, store, files = machine.facets_counts(), machine.store_counts(), machine.snapshot()
    customer = Machine(machine.work)
    customer.env.update(setting)
    for script, *args in (("seed.cmd", "--small"), ("bootstrap.cmd",), ("first-run.cmd",),
                          ("upgrade.cmd",), ("demo.cmd",)):
        refused = customer.run(script, *args)
        assert refused.returncode != 0, f"{script} ran on a customer machine"
        assert "set up as a customer's" in refused.output, refused.output[-2000:]
    assert machine.facets_counts() == facets
    assert machine.store_counts() == store
    assert machine.snapshot() == files
