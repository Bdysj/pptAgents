"""Durable, bounded-concurrency job queue with a fixed worker pool.

Design:
  * Submitting a job inserts a PENDING row (jobs.create) and enqueues its id.
  * A pool of ``PPT_MAX_CONCURRENCY`` workers pulls ids, *atomically* claims the
    job (PENDING -> CONVERTING) so no job runs twice, and executes process_job
    in a dedicated thread pool sized to the concurrency (so 20 blocking agent
    subprocesses can truly run in parallel — the default asyncio thread pool is
    too small for that).
  * The in-memory queue is just a fast wakeup channel; the DB is the source of
    truth. On startup, reconcile() re-enqueues PENDING jobs and marks orphaned
    in-flight jobs (left non-terminal by a previous crash) as INTERRUPTED.
  * A periodic reaper expires unfinished jobs older than PPT_JOB_EXPIRE_DAYS,
    cleaning their R2 objects and local folders.

Single-process assumption: the worker pool lives in this process. Horizontal
scaling would require each instance to claim from the shared DB (the atomic
claim already supports that) and to drop the in-memory queue in favor of polling.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from . import storage
from .config import settings
from .jobs import store
from .registry import registry
from .schemas import ACTIVE_STATUSES, JobStatus
from .worker import _cleanup_local, process_job

logger = logging.getLogger("ppt.queue")

_queue: asyncio.Queue[str] | None = None
_workers: list[asyncio.Task] = []
_reaper_task: asyncio.Task | None = None
_executor: ThreadPoolExecutor | None = None


def _ensure_queue() -> asyncio.Queue[str]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def enqueue(job_id: str) -> None:
    await _ensure_queue().put(job_id)


def enqueue_threadsafe(job_id: str) -> None:
    """Enqueue from a non-async context (e.g. a worker thread)."""
    loop = asyncio.get_event_loop()
    loop.call_soon_threadsafe(lambda: _ensure_queue().put_nowait(job_id))


async def _worker_loop(worker_id: int) -> None:
    queue = _ensure_queue()
    loop = asyncio.get_running_loop()
    while True:
        job_id = await queue.get()
        try:
            claimed = store.claim_pending(job_id)
            if claimed is None:
                # Already taken, cancelled, or retried elsewhere — skip.
                continue
            await loop.run_in_executor(_executor, process_job, job_id)
        except Exception:  # noqa: BLE001
            logger.exception("worker %s failed on job %s", worker_id, job_id)
        finally:
            queue.task_done()


async def reconcile() -> None:
    """On startup: re-enqueue PENDING jobs, mark orphaned in-flight jobs as
    INTERRUPTED (their owning process is gone; the registry is empty here)."""
    for job_id in store.list_active_ids():
        job = store.get(job_id)
        if job is None:
            continue
        if job.status == JobStatus.PENDING:
            await enqueue(job_id)
        elif not registry.is_running(job_id):
            store.update(
                job_id, status=JobStatus.INTERRUPTED,
                error="service restarted while this job was in progress",
                message="服务重启导致任务中断，已标记为未完成（可手动重试续跑）。",
            )


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(settings.reaper_interval_seconds)
        try:
            await asyncio.to_thread(_reap_once)
        except Exception:  # noqa: BLE001
            logger.exception("reaper pass failed")


def _reap_once() -> None:
    stale = store.list_stale_unfinished(settings.job_expire_days)
    for job in stale:
        handle = registry.get(job.job_id)
        if handle is not None:
            handle.request_cancel()
        try:
            storage.delete_prefix(job.job_id)
        except Exception:
            pass
        _cleanup_local(job.project_path, job.upload_path)
        store.update(
            job.job_id, status=JobStatus.EXPIRED, project_path=None, pptx_path=None,
            message=f"超过 {settings.job_expire_days} 天未完成，已自动清理。",
        )
        logger.info("reaped stale job %s", job.job_id)


async def start() -> None:
    """Start the worker pool + reaper. Call once on startup (after reconcile)."""
    global _executor, _reaper_task
    n = max(1, settings.max_concurrency)
    _executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="ppt-job")
    _ensure_queue()
    for i in range(n):
        _workers.append(asyncio.create_task(_worker_loop(i)))
    _reaper_task = asyncio.create_task(_reaper_loop())
    logger.info("queue started: %d workers, reaper every %ds",
                n, settings.reaper_interval_seconds)


async def shutdown() -> None:
    for task in _workers:
        task.cancel()
    if _reaper_task:
        _reaper_task.cancel()
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)
