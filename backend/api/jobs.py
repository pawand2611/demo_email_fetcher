"""Background jobs: sync and re-classify return at once and the client polls.

One job runs at a time, whatever its kind: asking for another while one is
running returns the running job instead of starting a second, so two clicks
never fetch the mailbox twice and re-classifying never races a sync.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"

KIND_SYNC = "sync"
KIND_RECLASSIFY = "reclassify"
KIND_REDECIDE = "redecide"


@dataclass
class Job:
    id: str
    kind: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)


class Jobs:
    def __init__(self, keep: int = 20) -> None:
        self._keep = keep
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._running: Job | None = None

    def start(self, kind: str, run: Callable[[], Any]) -> tuple[Job, bool]:
        """Start ``run`` in the background, or return the job already running. Returns (job, started)."""
        with self._lock:
            if self._running is not None:
                return self._running, False
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, state=STATE_RUNNING, started_at=datetime.now(timezone.utc))
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
            self._running = job
        threading.Thread(target=self._execute, args=(job, run), name=f"{kind}-{job.id}", daemon=True).start()
        return job, True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self, kind: str | None = None) -> Job | None:
        with self._lock:
            return next((j for j in reversed(self._jobs.values()) if kind is None or j.kind == kind), None)

    @property
    def running(self) -> Job | None:
        with self._lock:
            return self._running

    def _execute(self, job: Job, run: Callable[[], Any]) -> None:
        try:
            job.result = run()
            job.state = STATE_SUCCEEDED
        except Exception as exc:  # a crash must not leave the job "running" forever
            logger.exception("%s job %s crashed", job.kind, job.id)
            job.error = f"{type(exc).__name__}: {exc}"
            job.state = STATE_FAILED
        finally:
            job.finished_at = datetime.now(timezone.utc)
            with self._lock:
                self._running = None
            job.done.set()
