from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aura.drones.definition import DroneDefinition
from aura.drones.sync_runner import run_read_only_drone_sync

logger = logging.getLogger(__name__)

DEFAULT_CHECK_WAIT_CAP = 10


@dataclass
class DroneJob:
    run_id: str
    drone_id: str
    drone_name: str
    goal: str
    status: str = "queued"
    summary: str = ""
    tool_calls_made: int = 0
    tool_errors: int = 0
    elapsed_seconds: float = 0.0
    receipt: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    _drone_def: DroneDefinition | None = field(default=None, repr=False)
    _upstream: dict[str, Any] | None = field(default=None, repr=False)
    _completion_event: threading.Event = field(default_factory=threading.Event, repr=False)


class ReadOnlyDroneBackgroundRunner:
    def __init__(self, workspace_root: Path, max_parallel: int = 3) -> None:
        self._workspace_root = workspace_root
        self._max_parallel = max_parallel
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="drone-bg",
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, DroneJob] = {}

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status == "running")

    @property
    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status == "queued")

    def launch(
        self,
        drone: DroneDefinition,
        goal: str,
        *,
        upstream: dict[str, Any] | None = None,
    ) -> DroneJob:
        job = DroneJob(
            run_id=uuid.uuid4().hex[:12],
            drone_id=drone.id,
            drone_name=drone.name,
            goal=goal,
            _drone_def=drone,
            _upstream=upstream,
        )
        with self._lock:
            self._jobs[job.run_id] = job
        self._drain_queue()
        return job

    def get(self, run_id: str, wait_seconds: float = 0) -> DroneJob | None:
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                return None
            if job.status in ("completed", "failed", "timed_out", "cancelled"):
                return job
            if wait_seconds <= 0:
                return job
            capped = min(wait_seconds, DEFAULT_CHECK_WAIT_CAP)
            event = job._completion_event

        event.wait(timeout=capped)
        with self._lock:
            return self._jobs.get(run_id)

    def _drain_queue(self) -> None:
        """Start queued jobs if slots are available."""
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            queued_ids = sorted(
                jid for jid, j in self._jobs.items()
                if j.status == "queued" and j._drone_def is not None
            )
            for run_id in queued_ids:
                if running >= self._max_parallel:
                    break
                job = self._jobs[run_id]
                drone_def = job._drone_def
                if drone_def is not None:
                    self._start_job(job, drone_def)
                    running += 1

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def _start_job(self, job: DroneJob, drone: DroneDefinition) -> None:
        import time

        job.status = "running"
        job.started_at = time.time()
        self._executor.submit(self._run_job, job, drone)

    def _run_job(self, job: DroneJob, drone: DroneDefinition) -> None:
        try:
            result = run_read_only_drone_sync(
                workspace_root=self._workspace_root,
                drone_id=drone.id,
                drone=drone,
                goal=job.goal,
                timeout_seconds=drone.budget.timeout_seconds,
                upstream=job._upstream,
            )
            job.status = result.get("status", "completed")
            job.summary = result.get("summary", "")
            job.tool_calls_made = result.get("tool_calls_made", 0)
            job.tool_errors = result.get("tool_errors", 0)
            job.elapsed_seconds = result.get("elapsed_seconds", 0.0)
            job.receipt = result.get("receipt")
        except Exception as exc:
            logger.exception("Background drone job %s failed", job.run_id)
            job.status = "failed"
            job.error = str(exc)
        finally:
            import time

            job.ended_at = time.time()
            job._completion_event.set()
            self._drain_queue()
            logger.info(
                "Drone job %s (%s) finished: %s",
                job.run_id,
                job.drone_name,
                job.status,
            )

_runners: dict[Path, ReadOnlyDroneBackgroundRunner] = {}
_runner_lock = threading.Lock()


def get_background_runner(workspace_root: Path) -> ReadOnlyDroneBackgroundRunner:
    root = workspace_root.resolve()
    with _runner_lock:
        if root not in _runners:
            _runners[root] = ReadOnlyDroneBackgroundRunner(root)
        return _runners[root]
