"""In-process registry of currently-running jobs.

Holds the live runtime handle for each job this process is actively executing:
the agent subprocess (for cancellation), a cancel flag, the project path (for
progress sampling), and a coarse phase label. This is the source of truth for
"is this job running *right now, here*" — used by the status endpoint (live
overlay), the cancel endpoint (kill the process group), and crash detection
(a non-terminal DB row absent from this registry after restart is an orphan).

Single-process / single-container assumption: the registry is per-process. If
the service is ever scaled to multiple containers, liveness must additionally
fall back to the DB heartbeat (which is already written while jobs run).
"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import Popen


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunHandle:
    job_id: str
    project_path: Path | None = None
    phase: str = "starting"
    started_at: datetime = field(default_factory=_now)
    proc: Popen | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def request_cancel(self) -> None:
        """Flag cancellation and kill the agent's process group if running."""
        self.cancel_event.set()
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            # The agent runs with start_new_session=True, so it leads its own
            # process group; kill the whole group to take down child processes.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()

    def register(self, job_id: str, project_path: Path | None = None) -> RunHandle:
        handle = RunHandle(job_id=job_id, project_path=project_path)
        with self._lock:
            self._runs[job_id] = handle
        return handle

    def get(self, job_id: str) -> RunHandle | None:
        with self._lock:
            return self._runs.get(job_id)

    def unregister(self, job_id: str) -> None:
        with self._lock:
            self._runs.pop(job_id, None)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._runs

    def active_ids(self) -> set[str]:
        with self._lock:
            return set(self._runs.keys())


registry = RunRegistry()
