"""Source setup: name a source, read it, and shape its proposal.

Thin on purpose: every rule is in sdg/accelerator (workbench.py and the
modules it calls), and a refusal reaches the screen in the engine's own
words -- 409 for what the state of the source refuses (a login that can
write, a proposal older than the landscape, a protected database, a read
already running), 400 for a request that cannot be one (a name that is not
a source's, a level the record kind does not have).

Reading and proposing send statements to the source and can take minutes,
so they run as jobs (sdg/api/jobs.py) and the screen polls. A login that can
write is never gone on with silently: the request says so, `allow_write_login`.
Every edit hands back the whole plan with its tables derived again, so the
screen's table list always matches its hierarchy.

Phase 2 adds the plan's criteria (measuring them is a job: it counts rows in
the source), its structure and privacy classes, the pack's name, approval
(submit, approve, withdraw, kept in the store) and building the pack (a job).
A built file is read by its name from the list a build writes, exactly: no
path a request gives is ever opened.

Every refusal carries a kind (`refusal`), so the screen can say what to do
next in its own words.
"""

from __future__ import annotations

import logging
import threading
from typing import Annotated, Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from sdg.accelerator import approval, builder, criteria, loading, sources, workbench
from sdg.accelerator import plan as planning
from sdg.accelerator.source_read import NotARead, error_said
from sdg.api import jobs
from sdg.config import ProtectedDatabase
from sdg.discovery.errors import is_database_error

# A column's name as a request may give it.
ColumnName = Annotated[str, Field(min_length=1, max_length=130)]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/accelerator", tags=["source setup"])

KIND = "accelerator"


class Refusal(Exception):
    """A refusal with its status, its message and what kind it is, so the
    screen can offer a way on (write_login, stale, not_ready, approver...)."""

    def __init__(self, status: int, message: str, kind: str):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status,
                            content={"detail": self.message, "refusal": self.kind})


def _name(name: str) -> str:
    try:
        return sources.plain_name(name)
    except sources.SourceError as exc:
        raise Refusal(400, str(exc), "source_name") from exc


def _logged(name: str) -> str:
    """A source's name as a log line may hold it: its plain form, or, for a
    name that is not one, a short quoted form (no line breaks reach the log)."""
    try:
        return sources.plain_name(name)
    except sources.SourceError:
        return repr(name)[:80]


def _approval_kind(message: str) -> str:
    """An approval refused because the plan changed, or for its state."""
    return "changed" if "changed since" in message else "approval"


def _said(name: str, call: Callable[[], Any]) -> Any:
    """Run one call into the engine, its refusals turned into statuses, in
    the screen's words (workbench.screen_words: no command-line advice). Any
    other failure is said, and logged, by its kind and codes only: its own
    text can quote a value from the source."""
    try:
        return call()
    except Refusal:
        raise
    except sources.WriteLogin as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), "write_login") from exc
    except workbench.Conflict as exc:
        raise Refusal(exc.status, str(exc), exc.kind or "conflict") from exc
    except workbench.StoreFailed as exc:
        log.warning("Source setup on %s: the store failed: %s", _logged(name),
                    error_said(exc.__cause__ or exc))
        raise Refusal(502, str(exc), "store") from None
    except ProtectedDatabase as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), "protected") from exc
    except NotARead as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), "not_a_read") from exc
    except sources.SourceError as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), "source") from exc
    except approval.ApproverRefused as exc:
        raise Refusal(403, str(exc), "approver") from exc
    except approval.ApprovalRefused as exc:
        raise Refusal(409, str(exc), _approval_kind(str(exc))) from exc
    except builder.BuildRefused as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), "build") from exc
    except loading.LoadRefused as exc:
        raise Refusal(409, workbench.screen_words(str(exc)), exc.kind) from exc
    except ValueError as exc:
        raise Refusal(400, workbench.screen_words(str(exc)), "invalid") from exc
    except Exception as exc:
        log.warning("Source setup on %s failed: %s", _logged(name), error_said(exc))
        if is_database_error(exc):
            raise Refusal(502, workbench.unreadable(name, exc), "unreadable") from None
        raise Refusal(500, f"Source setup on {name} failed ({error_said(exc)}). See the "
                           f"server log for where it stopped.", "failed") from None


def _answer(name: str, call: Callable[[], Any]) -> Any:
    try:
        return _said(name, call)
    except Refusal as refused:
        return refused.response()


# The engine's own refusals: their words are its, naming tables, columns and
# settings, never a value read from the source.
ENGINE_REFUSALS = (sources.SourceError, workbench.Conflict, workbench.StoreFailed,
                   ProtectedDatabase, NotARead, approval.ApprovalRefused,
                   approval.ApproverRefused, builder.BuildRefused, loading.LoadRefused)


def _in_job(name: str, work: Callable[[jobs.Progress], Any],
            failed: str = "could not be read or proposed") -> Callable[[jobs.Progress], Any]:
    """A job's work. Only the engine's own refusals are said as written; any
    other failure -- a driver's, or one raised while reading values -- is a
    fixed sentence with its kind and codes (error_said), since its own text
    can quote a value from the source. Logged the same way, for that reason."""
    def run(progress: jobs.Progress) -> Any:
        try:
            return work(progress)
        except ENGINE_REFUSALS as exc:
            raise jobs.Refused(workbench.screen_words(str(exc))) from exc
        except Exception as exc:
            log.warning("Source setup job on %s failed: %s", _logged(name), error_said(exc))
            if is_database_error(exc):
                raise jobs.Refused(workbench.unreadable(name, exc)) from None
            raise jobs.Refused(f"{name} {failed} ({error_said(exc)}). "
                               f"See the server log for where it stopped.") from None
    return run


def _stage(progress: jobs.Progress) -> workbench.Stage:
    def told(done: int, total: int, words: str) -> None:
        progress.set_total(total, words)
        progress.done = done
    return told


# Checking that no read runs and starting one are a single step: held from
# the check until the job is registered, so two requests cannot both start.
_starting = threading.Lock()


# Which source each job reads, so the screen locks only that source's work.
_job_sources: dict[str, str] = {}


def _job_view(job: jobs.Job) -> dict[str, Any]:
    return {**job.as_dict(), "source": _job_sources.get(job.id)}


def _start(source: str, label: str, work: Callable[[jobs.Progress], Any],
           failed: str = "could not be read or proposed") -> dict[str, Any]:
    job = jobs.start(KIND, label, _in_job(source, work, failed))
    _job_sources[job.id] = source
    for gone in [i for i in _job_sources if jobs.get(i) is None]:
        _job_sources.pop(gone, None)
    return _job_view(job)


def _one_at_a_time() -> None:
    if jobs.running(KIND):
        raise Refusal(409, "A source is being read, proposed, measured, built or loaded "
                           "already. Wait for it to finish.", "busy")


# --- sources ------------------------------------------------------------------

@router.get("/sources")
def list_sources() -> dict[str, Any]:
    """The sources named in .env, added this session, or read before."""
    return {"sources": workbench.list_sources()}


class AddSource(BaseModel):
    name: str = Field(..., max_length=64)


@router.post("/sources")
def add_source(req: AddSource) -> Any:
    """Add a source by name for this session; its settings come from .env.
    No login or password is sent from the screen."""
    return _answer(req.name, lambda: {"sources": workbench.add_source(_name(req.name))})


class ReadRequest(BaseModel):
    # Go on although the login can write (it is still only read). Never assumed.
    allow_write_login: bool = False
    # Limit on the profile's pairwise measures; the installation's by default.
    minutes: float | None = Field(None, gt=0, le=240)


@router.post("/sources/{name}/read")
def read_source(name: str, req: ReadRequest) -> Any:
    """Start reading the source's landscape, and proposing from it when the
    source has no proposal yet. The login is checked before the job starts,
    so its refusal is an answer, not a failed job."""
    def start() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            registry = workbench.open_source(source, allow_write_login=req.allow_write_login)
            return _start(source, f"Read {source}", lambda progress: workbench.read_source(
                source, registry, _stage(progress), minutes=req.minutes))
    return _answer(name, start)


class ProposeRequest(BaseModel):
    # Replace the saved proposal, and every choice made in it.
    force: bool = False
    allow_write_login: bool = False
    # Propose from the saved landscape alone, sending nothing to the source.
    offline: bool = False


@router.post("/sources/{name}/propose")
def propose(name: str, req: ProposeRequest) -> Any:
    """Start proposing from the saved landscape. Refused when a proposal is
    saved, unless `force`."""
    def start() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            workbench.saved_landscape(source)
            workbench.refuse_replacing(source, req.force)
            registry = None if req.offline else workbench.open_source(
                source, allow_write_login=req.allow_write_login)
            return _start(source, f"Propose {source}", lambda progress:
                          workbench.propose_source(source, registry, _stage(progress)))
    return _answer(name, start)


@router.get("/jobs/latest")
def latest_job() -> dict[str, Any]:
    """The read or proposal last started: in flight, or its outcome."""
    job = jobs.latest(KIND)
    return _job_view(job) if job else {"state": "none"}


@router.get("/jobs/{job_id}")
def read_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None or job.kind != KIND:
        raise HTTPException(
            status_code=404,
            detail="restart_lost: no such job. The server may have restarted; a read in "
                   "flight was lost. Read the source again.")
    return _job_view(job)


@router.get("/sources/{name}/landscape")
def landscape_summary(name: str) -> Any:
    """What the last read found; `summary` is null when the source was never read."""
    return _answer(name, lambda: {"source": _name(name),
                                  "summary": workbench.landscape_summary(_name(name))})


# --- the proposal ---------------------------------------------------------------

@router.get("/sources/{name}/proposal")
def proposal(name: str) -> Any:
    """The saved proposal, with the alternatives for its levels; `plan` is
    null when there is none yet."""
    def held() -> dict[str, Any]:
        source = _name(name)
        found = workbench.proposal(source)
        return found if found is not None else {"source": source, "plan": None, "stale": False,
                                                "stale_reason": None, "alternatives": {}}
    return _answer(name, held)


def _edit(name: str, change: Callable[[planning.SourcePlan, Any], planning.SourcePlan]) -> Any:
    return _answer(name, lambda: workbench.edit(_name(name), change))


class ChooseRecord(BaseModel):
    role: str
    table: str


@router.post("/sources/{name}/proposal/choose-record")
def choose_record(name: str, req: ChooseRecord) -> Any:
    return _edit(name, lambda held, graph: planning.choose_record(held, req.role, req.table,
                                                                  graph=graph))


class SelectLevel(BaseModel):
    role: str
    level_id: str
    selected: bool


@router.post("/sources/{name}/proposal/select-level")
def select_level(name: str, req: SelectLevel) -> Any:
    return _edit(name, lambda held, graph: planning.select_level(
        held, req.role, req.level_id, req.selected, graph=graph))


class ReorderLevels(BaseModel):
    role: str
    ids: list[str]


@router.post("/sources/{name}/proposal/reorder")
def reorder(name: str, req: ReorderLevels) -> Any:
    return _edit(name, lambda held, graph: planning.reorder_levels(held, req.role, req.ids,
                                                                   graph=graph))


class SwapLevel(BaseModel):
    role: str
    level_id: str
    table: str
    column: str


@router.post("/sources/{name}/proposal/swap")
def swap(name: str, req: SwapLevel) -> Any:
    """Swap a level for a column not yet a level, or for an unselected level
    the proposal offers (workbench.swap): one step either way."""
    return _edit(name, lambda held, graph: workbench.swap(
        held, req.role, req.level_id, req.table, req.column, graph=graph))


class NameRecord(BaseModel):
    role: str
    one: str
    many: str


@router.post("/sources/{name}/proposal/name")
def name_record(name: str, req: NameRecord) -> Any:
    return _edit(name, lambda held, graph: planning.name_record(held, req.role, req.one,
                                                                req.many))


class StateJoin(BaseModel):
    role: str = Field(..., max_length=20)
    from_table: str = Field(..., max_length=260)
    from_columns: list[ColumnName] = Field(..., min_length=1, max_length=16)
    to_table: str = Field(..., max_length=260)
    to_columns: list[ColumnName] = Field(..., min_length=1, max_length=16)


@router.post("/sources/{name}/proposal/join")
def state_join(name: str, req: StateJoin) -> Any:
    """State a join inference did not find, or the one join the child follows
    to its primary where several are found (each column checked against the
    model)."""
    from sdg.accelerator.hierarchy import Join

    join = Join(req.from_table, list(req.from_columns), req.to_table, list(req.to_columns),
                "stated", None)
    return _edit(name, lambda held, graph: planning.state_join(held, req.role, join,
                                                               graph=graph))


class SelectFeature(BaseModel):
    role: str
    feature_id: str
    selected: bool


@router.post("/sources/{name}/proposal/select-feature")
def select_feature(name: str, req: SelectFeature) -> Any:
    return _edit(name, lambda held, graph: planning.select_feature(
        held, req.role, req.feature_id, req.selected))


# --- phase 2: criteria ------------------------------------------------------------

@router.get("/sources/{name}/criteria")
def criteria_view(name: str) -> Any:
    """Each record kind's criteria, with the share each keeps where measured
    (`estimated` where it was read from a sample)."""
    return _answer(name, lambda: workbench.criteria_view(_name(name)))


class SelectCriterion(BaseModel):
    id: str = Field(..., max_length=300)
    selected: bool


@router.post("/sources/{name}/criteria/select")
def select_criterion(name: str, req: SelectCriterion) -> Any:
    return _edit(name, lambda held, graph: planning.select_criterion(
        held, req.id, req.selected, graph=graph))


class SetCriterion(BaseModel):
    id: str = Field(..., max_length=300)
    # Typed by the person: each checked as a literal for its column, and the
    # count, op and months by the engine (criteria.check), which says why.
    values: list[str] | None = Field(None, max_length=criteria.VALUES_MAX * 20)
    op: str | None = Field(None, max_length=20)
    months: int | None = None


@router.post("/sources/{name}/criteria/set")
def set_criterion(name: str, req: SetCriterion) -> Any:
    return _edit(name, lambda held, graph: planning.set_criterion(
        held, req.id, values=req.values, months=req.months, op=req.op, graph=graph))


class MeasureRequest(BaseModel):
    allow_write_login: bool = False


@router.post("/sources/{name}/criteria/measure")
def measure_criteria(name: str, req: MeasureRequest) -> Any:
    """Start counting the share of rows each criterion keeps: one count each,
    read from the source. The login is checked before the job starts."""
    def start() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            workbench.refuse_measuring(source)
            registry = workbench.open_source(source, allow_write_login=req.allow_write_login)
            return _start(source, f"Measure {source}", lambda progress:
                          workbench.measure_criteria(source, registry, _stage(progress)),
                          "could not be measured")
    return _answer(name, start)


# --- phase 2: structure and the pack's name -----------------------------------------

@router.get("/sources/{name}/structure")
def structure_view(name: str) -> Any:
    """The denorm tables: every column, what it serves, its privacy class."""
    return _answer(name, lambda: workbench.structure_view(_name(name)))


class SetPrivacy(BaseModel):
    table: str = Field(..., max_length=260)
    column: str = Field(..., max_length=130)
    # Checked by the engine (structure.refusal), which says why.
    privacy: str = Field(..., max_length=20)
    # The person's word that a key proposed as a surrogate holds nothing
    # personal: only then is it kept.
    confirm_not_personal: bool = False


@router.post("/sources/{name}/structure/privacy")
def set_privacy(name: str, req: SetPrivacy) -> Any:
    """A column's privacy class. A locked column is never made kept."""
    return _edit(name, lambda held, graph: planning.set_privacy(
        held, req.table, req.column, req.privacy, graph=graph,
        confirm_not_personal=req.confirm_not_personal))


class AcceptGrain(BaseModel):
    role: str = Field(..., max_length=20)
    table: str = Field(..., max_length=260)
    accept: bool = True


@router.post("/sources/{name}/structure/grain")
def accept_grain(name: str, req: AcceptGrain) -> Any:
    """Accept, or take back, that a record kind's table repeats each record
    once per row of a link table entered without dates."""
    return _edit(name, lambda held, graph: planning.accept_grain(
        held, req.role, req.table, req.accept, graph=graph))


class AddExtra(BaseModel):
    role: str = Field(..., max_length=20)
    table: str = Field(..., max_length=260)
    column: str = Field(..., max_length=130)


@router.post("/sources/{name}/structure/extra")
def add_extra(name: str, req: AddExtra) -> Any:
    """Add a column, from a table the record kind reads, to its denorm table."""
    return _edit(name, lambda held, graph: planning.add_extra_column(
        held, req.role, req.table, req.column, graph=graph))


class NamePack(BaseModel):
    id: str = Field(..., max_length=40)
    name: str = Field(..., max_length=100)


@router.post("/sources/{name}/pack-name")
def name_pack(name: str, req: NamePack) -> Any:
    return _edit(name, lambda held, graph: planning.name_pack(held, req.id, req.name))


# --- phase 2: approval --------------------------------------------------------------

@router.get("/sources/{name}/approval")
def approval_view(name: str) -> Any:
    """Where the plan stands, checked against the plan saved now: approved
    only while it is the plan that was approved."""
    return _answer(name, lambda: workbench.approval_view(_name(name)))


@router.post("/sources/{name}/submit")
def submit(name: str) -> Any:
    return _answer(name, lambda: workbench.submit(_name(name)))


class ApproveRequest(BaseModel):
    # The hash of the plan the screen showed: approved only as it was looked at.
    plan_hash: str = Field(..., min_length=64, max_length=64)
    # The second approver's name, typed to confirm, when one is set.
    approver: str | None = Field(None, max_length=100)


@router.post("/sources/{name}/approve")
def approve(name: str, req: ApproveRequest) -> Any:
    return _answer(name, lambda: workbench.approve(_name(name), req.plan_hash, req.approver))


@router.post("/sources/{name}/withdraw")
def withdraw(name: str) -> Any:
    return _answer(name, lambda: workbench.withdraw(_name(name)))


# --- phase 2: build ------------------------------------------------------------------

@router.post("/sources/{name}/build")
def build(name: str) -> Any:
    """Start building the approved plan's pack. What a build would refuse is
    asked first, so it is an answer, not a failed job."""
    def start() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            workbench.refuse_building(source)
            return _start(source, f"Build {source}", lambda progress:
                          workbench.build_pack(source, _stage(progress)), "could not be built")
    return _answer(name, start)


@router.get("/sources/{name}/files")
def built_files(name: str) -> Any:
    """The files a build writes, each with whether it is built."""
    return _answer(name, lambda: workbench.built_files(_name(name)))


@router.get("/sources/{name}/files/{file:path}")
def built_file(name: str, file: str, download: bool = False) -> Any:
    """One built file, as plain text, by its name from the list a build
    writes. Shown, never rendered: text/plain, and the browser is told not
    to guess otherwise; with `download`, saved under its own name."""
    def read() -> PlainTextResponse:
        listed, text = workbench.built_file(_name(name), file)
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                   "Content-Security-Policy": "default-src 'none'; sandbox"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{listed.rsplit("/", 1)[-1]}"'
        return PlainTextResponse(text, headers=headers)
    return _answer(name, read)


# --- phase 3: create the tables, load, refresh -----------------------------------

@router.get("/sources/{name}/load")
def load_status(name: str) -> Any:
    """The built pack, the store's tables, whether SDM runs on that pack, and
    the latest loads."""
    return _answer(name, lambda: workbench._store(lambda: loading.status(_name(name))))


@router.post("/sources/{name}/store-tables")
def create_tables(name: str) -> Any:
    """Create the built tables in the store, add-only, from store_tables.sql
    exactly as built. Refused while a job runs: a load may be filling them."""
    def create() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            return workbench._store(lambda: loading.create_tables(source))
    return _answer(name, create)


class LoadRequest(BaseModel):
    # Go on although the source's login can write (the load only reads). Never assumed.
    allow_write_login: bool = False


@router.post("/sources/{name}/load")
def load(name: str, req: LoadRequest) -> Any:
    """Start loading (or refreshing) the built tables from the source. The
    pack, the store and the source's login are checked before the job starts,
    so their refusals are answers, not failed jobs."""
    def start() -> dict[str, Any]:
        source = _name(name)
        with _starting:
            _one_at_a_time()
            workbench._store(lambda: loading.refuse_loading(source))
            checker = workbench.open_source(source, allow_write_login=req.allow_write_login)

            def work(progress: jobs.Progress) -> dict[str, Any]:
                return loading.load(source, _stage(progress), lambda: progress.canceled,
                                    checker=checker)
            return _start(source, f"Load {source}", work, "could not be loaded")
    return _answer(name, start)


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict[str, Any]:
    """Ask a running job to stop. A load stops after the table it is on: the
    tables after it keep their rows."""
    job = jobs.get(job_id)
    if job is None or job.kind != KIND:
        raise HTTPException(status_code=404, detail="No such job.")
    return {"asked": jobs.cancel(job_id), **_job_view(job)}
