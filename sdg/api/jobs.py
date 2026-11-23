"""Work too long to hold a request open for.

Enumerating the pattern baseline runs one clustering pass per hierarchy
combination. Measured on the demo database that is 1.8 seconds a combination
for members and 8.4 for claims, and a full-depth walk is 340 and 329
combinations: ten minutes and three quarters of an hour. No browser will hold
a request open that long, and nobody should watch a spinner for it either.

So the walk runs on a thread and the caller polls. The registry is a dictionary
in this process, which is the right size for a tool that runs on one laptop as
one process: there is no queue to configure and no state to migrate. It also
means a restart forgets everything in flight, which is stated rather than
hidden — `restart_lost` is what a client sees for a job it was polling before.

Cancellation is cooperative. The worker is handed a handle and checks it
between units of work, because killing a thread mid-clustering would leave
pandas and the database connection in whatever state they happened to be in.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sdg.discovery.errors import plain

# Finished jobs are kept so a client that polls late still learns the outcome.
# Beyond this many, the oldest finished ones go; running jobs are never dropped.
KEEP_FINISHED = 20


@dataclass
class Progress:
    """The handle a worker reports through.

    `total` is set once the worker knows how much there is to do, which for the
    baseline is after enumeration rather than at the start -- so a job is
    briefly running with no total, and the screen says "enumerating" rather
    than showing a progress bar that is lying.
    """

    total: Optional[int] = None
    done: int = 0
    note: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event)

    def step(self, n: int = 1, note: str = "") -> None:
        self.done += n
        if note:
            self.note = note

    def set_total(self, total: int, note: str = "") -> None:
        self.total = total
        if note:
            self.note = note

    @property
    def canceled(self) -> bool:
        return self._cancel.is_set()

    def raise_if_canceled(self) -> None:
        if self.canceled:
            raise Canceled()


class Canceled(Exception):
    """Raised inside a worker that has been asked to stop."""


class Refused(Exception):
    """Raised inside a worker for a refusal already said in the person's
    words: the job's error is the message as it stands, with no type name."""


@dataclass
class Job:
    id: str
    kind: str
    label: str
    state: str                      # running | done | failed | canceled
    started_at: float
    progress: Progress
    finished_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None

    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at

    def as_dict(self) -> dict:
        elapsed = self.elapsed()
        done, total = self.progress.done, self.progress.total

        # Only from measured work. An estimate made before anything has
        # finished is a guess dressed as a number.
        eta = None
        if self.state == "running" and total and done:
            eta = max(0.0, (elapsed / done) * (total - done))

        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "done": done,
            "total": total,
            "note": self.progress.note,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "result": self.result,
            "error": self.error,
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def start(kind: str, label: str, work: Callable[[Progress], Any]) -> Job:
    """Run `work` on a thread, handing it a progress handle. Returns at once."""
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        label=label,
        state="running",
        started_at=time.monotonic(),
        progress=Progress(),
    )

    def run() -> None:
        try:
            result = work(job.progress)
        except Canceled:
            job.state, job.finished_at = "canceled", time.monotonic()
        except Refused as e:
            job.state, job.error = "failed", str(e)
            job.finished_at = time.monotonic()
        except Exception as e:  # the worker's failure is the job's result
            job.state = "failed"
            job.error = f"{plain(e)}"
            job.finished_at = time.monotonic()
        else:
            job.state, job.result = "done", result
            job.finished_at = time.monotonic()

    with _lock:
        _jobs[job.id] = job
        _prune_locked()

    threading.Thread(target=run, name=f"job-{kind}-{job.id}", daemon=True).start()
    return job


def get(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def cancel(job_id: str) -> bool:
    """Ask a job to stop. True if it was running and has been asked."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None or job.state != "running":
        return False
    job.progress._cancel.set()
    return True


def latest(kind: str) -> Optional[Job]:
    """The most recently started job of a kind, running or not.

    What a screen needs on load: either something is in flight to attach to,
    or the last outcome is worth showing.
    """
    with _lock:
        of_kind = [j for j in _jobs.values() if j.kind == kind]
    return max(of_kind, key=lambda j: j.started_at) if of_kind else None


def running(kind: Optional[str] = None) -> list[Job]:
    with _lock:
        return [
            j for j in _jobs.values()
            if j.state == "running" and (kind is None or j.kind == kind)
        ]


def _prune_locked() -> None:
    finished = sorted(
        (j for j in _jobs.values() if j.state != "running"),
        key=lambda j: j.finished_at or 0,
    )
    for job in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
        _jobs.pop(job.id, None)
