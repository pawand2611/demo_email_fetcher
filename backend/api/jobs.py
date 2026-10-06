"""Background sync jobs: Refresh returns at once and the client polls.

At most one sync runs at a time; asking for another while one is running
returns the running job instead of starting a second, so two clicks never
fetch the mailbox twice.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from mailbox_viewer.sync import SyncResult

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"


@dataclass
class SyncJob:
    id: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    result: SyncResult | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)


class SyncJobs:
    def __init__(self, run: Callable[[], SyncResult], keep: int = 20) -> None:
        self._run = run
        self._keep = keep
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, SyncJob] = OrderedDict()
        self._running: SyncJob | None = None

    def start(self) -> tuple[SyncJob, bool]:
        """Start a sync, or return the one already running. Returns (job, started)."""
        with self._lock:
            if self._running is not None:
                return self._running, False
            job = SyncJob(id=uuid.uuid4().hex[:12], state=STATE_RUNNING, started_at=datetime.now(timezone.utc))
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
            self._running = job
        threading.Thread(target=self._execute, args=(job,), name=f"sync-{job.id}", daemon=True).start()
        return job, True

    def get(self, job_id: str) -> SyncJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> SyncJob | None:
        with self._lock:
            return next(reversed(self._jobs.values()), None)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running is not None

    def _execute(self, job: SyncJob) -> None:
        try:
            result = self._run()
            job.result = result
            job.state = STATE_SUCCEEDED
        except Exception as exc:  # a crash must not leave the job "running" forever
            logger.exception("sync job %s crashed", job.id)
            job.error = f"{type(exc).__name__}: {exc}"
            job.state = STATE_FAILED
        finally:
            job.finished_at = datetime.now(timezone.utc)
            with self._lock:
                self._running = None
            job.done.set()
