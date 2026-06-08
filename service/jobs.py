"""Durable job store backed by a database (MySQL via SQLAlchemy).

The public interface (``create`` / ``get`` / ``update`` returning a :class:`Job`)
is unchanged from the original in-memory store. Additional helpers support the
worker pool (atomic claim), the reaper (stale listing), and crash reconciliation
(orphan listing). Each call uses a short-lived session; the returned :class:`Job`
is a plain snapshot detached from the DB.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update as sa_update

from .db import JobRow, session_factory, utcnow
from .schemas import ACTIVE_STATUSES, JobStatus


def _now() -> datetime:
    return utcnow()


@dataclass
class Job:
    job_id: str
    source_filename: str
    canvas_format: str
    upload_path: Path
    options: dict[str, str | None] = field(default_factory=dict)
    fingerprint: str | None = None
    status: JobStatus = JobStatus.PENDING
    message: str = ""
    project_path: Path | None = None
    pptx_path: Path | None = None
    r2_key: str | None = None
    r2_url: str | None = None
    notes_key: str | None = None
    notes_url: str | None = None
    error: str | None = None
    log_tail: str = ""
    progress: int = 0
    attempts: int = 0
    heartbeat_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


_SCALAR_FIELDS = (
    "source_filename", "canvas_format", "status", "message", "fingerprint",
    "r2_key", "r2_url", "notes_key", "notes_url", "error", "log_tail",
    "progress", "attempts", "heartbeat_at", "created_at", "updated_at",
)
_PATH_FIELDS = ("upload_path", "project_path", "pptx_path")


def _row_to_job(row: JobRow) -> Job:
    options: dict[str, str | None] = {}
    if row.options:
        try:
            options = json.loads(row.options)
        except (ValueError, TypeError):
            options = {}
    return Job(
        job_id=row.job_id,
        source_filename=row.source_filename or "",
        canvas_format=row.canvas_format or "",
        upload_path=Path(row.upload_path) if row.upload_path else Path(""),
        options=options,
        fingerprint=row.fingerprint,
        status=JobStatus(row.status),
        message=row.message or "",
        project_path=Path(row.project_path) if row.project_path else None,
        pptx_path=Path(row.pptx_path) if row.pptx_path else None,
        r2_key=row.r2_key,
        r2_url=row.r2_url,
        notes_key=row.notes_key,
        notes_url=row.notes_url,
        error=row.error,
        log_tail=row.log_tail or "",
        progress=row.progress or 0,
        attempts=row.attempts or 0,
        heartbeat_at=row.heartbeat_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class JobStore:
    """Database-backed job store. Thread-safe via short-lived sessions."""

    def create(self, source_filename: str, canvas_format: str, upload_path: Path,
               options: dict[str, str | None] | None = None,
               fingerprint: str | None = None) -> Job:
        job_id = uuid.uuid4().hex[:12]
        now = _now()
        Session = session_factory()
        with Session() as session:
            row = JobRow(
                job_id=job_id,
                source_filename=source_filename,
                canvas_format=canvas_format,
                fingerprint=fingerprint,
                status=JobStatus.PENDING.value,
                message="",
                upload_path=str(upload_path),
                pptx_filename=None,
                options=json.dumps(options or {}, ensure_ascii=False),
                progress=0,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _row_to_job(row)

    def find_reusable(self, fingerprint: str) -> Job | None:
        """Return the most recent job with the same fingerprint that is either
        in flight (PENDING/CONVERTING/GENERATING) or already DONE — so repeated
        identical submissions reuse it. FAILED/CANCELLED/INTERRUPTED/EXPIRED are
        intentionally NOT reused (a re-submit after those should start fresh)."""
        if not fingerprint:
            return None
        reusable = [s.value for s in ACTIVE_STATUSES] + [JobStatus.DONE.value]
        Session = session_factory()
        with Session() as session:
            row = session.execute(
                select(JobRow)
                .where(JobRow.fingerprint == fingerprint, JobRow.status.in_(reusable))
                .order_by(JobRow.created_at.desc())
                .limit(1)
            ).scalars().first()
            return _row_to_job(row) if row else None

    def get(self, job_id: str) -> Job | None:
        Session = session_factory()
        with Session() as session:
            row = session.get(JobRow, job_id)
            return _row_to_job(row) if row else None

    def update(self, job_id: str, **changes) -> Job | None:
        Session = session_factory()
        with Session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return None
            for key, value in changes.items():
                if key in _PATH_FIELDS:
                    setattr(row, key, str(value) if value is not None else None)
                    if key == "pptx_path":
                        row.pptx_filename = Path(value).name if value is not None else None
                elif key == "status" and isinstance(value, JobStatus):
                    row.status = value.value
                elif key == "options":
                    row.options = json.dumps(value or {}, ensure_ascii=False)
                elif key in _SCALAR_FIELDS or hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = _now()
            session.commit()
            return _row_to_job(row)

    def bump_progress(self, job_id: str, progress: int) -> None:
        """Set progress, but never let it move backwards (monotonic)."""
        Session = session_factory()
        with Session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return
            if progress > (row.progress or 0):
                row.progress = progress
                row.updated_at = _now()
                session.commit()

    def heartbeat(self, job_id: str) -> None:
        Session = session_factory()
        with Session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return
            row.heartbeat_at = _now()
            session.commit()

    def claim_pending(self, job_id: str) -> Job | None:
        """Atomically move PENDING -> CONVERTING so only one worker runs it.

        Returns the claimed Job, or None if another worker got there first or the
        job is no longer PENDING (e.g. cancelled while queued)."""
        Session = session_factory()
        with Session() as session:
            result = session.execute(
                sa_update(JobRow)
                .where(JobRow.job_id == job_id, JobRow.status == JobStatus.PENDING.value)
                .values(status=JobStatus.CONVERTING.value, updated_at=_now(),
                        heartbeat_at=_now())
            )
            session.commit()
            if result.rowcount != 1:
                return None
            row = session.get(JobRow, job_id)
            return _row_to_job(row) if row else None

    def list_active_ids(self) -> list[str]:
        """All jobs in a non-terminal state (for startup reconciliation)."""
        Session = session_factory()
        with Session() as session:
            rows = session.execute(
                select(JobRow.job_id, JobRow.status)
                .where(JobRow.status.in_([s.value for s in ACTIVE_STATUSES]))
            ).all()
            return [r[0] for r in rows]

    def list_stale_unfinished(self, older_than_days: int) -> list[Job]:
        """Unfinished jobs whose last update is older than the cutoff (reaper)."""
        cutoff = _now() - timedelta(days=older_than_days)
        Session = session_factory()
        with Session() as session:
            rows = session.execute(
                select(JobRow)
                .where(JobRow.status.in_([s.value for s in ACTIVE_STATUSES]))
                .where(JobRow.updated_at < cutoff)
            ).scalars().all()
            return [_row_to_job(r) for r in rows]

    def count_pending(self) -> int:
        Session = session_factory()
        with Session() as session:
            rows = session.execute(
                select(JobRow.job_id).where(JobRow.status == JobStatus.PENDING.value)
            ).all()
            return len(rows)


store = JobStore()
