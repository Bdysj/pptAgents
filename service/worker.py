"""Job orchestration: scaffold project -> import source -> drive agent -> deliver.

The deterministic front steps (init + source conversion via import-sources) run
here directly. The creative core (Step 4–7) is handed to the headless Claude Code
agent. On failure the agent is retried *with a resume prompt* against the same
project directory, so already-produced artifacts (notably per-page SVGs) are not
redone. After success the deck is uploaded to R2 and the local project is removed.

This module is invoked by the worker pool (after the job has been atomically
claimed PENDING -> CONVERTING) and runs in a worker thread.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import storage
from .agent_runner import run_agent
from .config import settings
from .jobs import store
from .progress import compute_progress
from .prompt_template import build_generation_prompt, build_resume_prompt
from .registry import RunHandle, registry
from .schemas import JobStatus
from .speaker_notes import build_speaker_notes_md

_INIT_PATH_RE = re.compile(r"Project (?:initialized|created):\s*(.+)$", re.MULTILINE)
_PPTX_READY_RE = re.compile(r"PPTX_READY:\s*(.+)$", re.MULTILINE)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(settings.repo_root),
        env=settings.agent_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _sanitize(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return cleaned or "deck"


def _init_project(job_id: str, source_filename: str, canvas_format: str) -> Path:
    project_name = f"job_{job_id}_{_sanitize(source_filename)}"[:60]
    proc = _run([
        settings.python_bin, str(settings.project_manager), "init", project_name,
        "--format", canvas_format, "--dir", str(settings.projects_dir),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"project init failed: {proc.stderr or proc.stdout}")
    match = _INIT_PATH_RE.search(proc.stdout)
    if not match:
        raise RuntimeError(f"could not parse project path from init output:\n{proc.stdout}")
    return Path(match.group(1).strip())


def _import_source(project_path: Path, upload_path: Path) -> None:
    proc = _run([
        settings.python_bin, str(settings.project_manager), "import-sources",
        str(project_path), str(upload_path), "--move",
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"import-sources failed: {proc.stderr or proc.stdout}")


def _find_pptx(project_path: Path, agent_stdout: str) -> Path | None:
    match = _PPTX_READY_RE.search(agent_stdout)
    if match:
        candidate = Path(match.group(1).strip())
        if candidate.is_file():
            return candidate
    exports = project_path / "exports"
    pptx_files = sorted(exports.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pptx_files[0] if pptx_files else None


def _start_heartbeat(job_id: str, handle: RunHandle) -> threading.Event:
    """Background heartbeat: refresh heartbeat_at + progress while the job runs,
    so zombies are detectable and progress advances without a client polling."""
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(settings.heartbeat_seconds):
            try:
                store.heartbeat(job_id)
                job = store.get(job_id)
                if job is not None:
                    pct = compute_progress(job.status, handle.project_path, job.options)
                    store.bump_progress(job_id, pct)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()
    return stop


def process_job(job_id: str) -> None:
    """Full pipeline for one job. Runs in a worker thread.

    Precondition: the job has been claimed (status CONVERTING). For retried jobs
    an existing project directory triggers resume mode.
    """
    job = store.get(job_id)
    if job is None:
        return

    handle = registry.register(job_id, project_path=job.project_path)
    heartbeat_stop = _start_heartbeat(job_id, handle)
    try:
        # Resume mode if a project dir already exists (retry of an interrupted run).
        resume = job.project_path is not None and Path(job.project_path).is_dir()
        if resume:
            project_path = Path(job.project_path)
            store.update(job_id, status=JobStatus.GENERATING,
                         message="检测到已有产物，从断点续跑…")
        else:
            store.update(job_id, status=JobStatus.CONVERTING, message="初始化项目并导入素材…")
            project_path = _init_project(job_id, job.source_filename, job.canvas_format)
            store.update(job_id, project_path=project_path)
            handle.project_path = project_path
            _import_source(project_path, job.upload_path)

        handle.project_path = project_path
        if handle.cancelled:
            return _handle_cancel(job_id, project_path)

        # Run the agent, retrying with a resume prompt on transient failures.
        result = _run_agent_with_retries(job_id, handle, project_path, job)

        if handle.cancelled or (result is not None and result.cancelled):
            return _handle_cancel(job_id, project_path)

        if result is None or result.returncode != 0 or result.timed_out:
            detail = "agent timed out" if (result and result.timed_out) else "agent failed"
            store.update(
                job_id, status=JobStatus.INTERRUPTED,
                error=f"{detail} after {job.attempts + settings.agent_retries + 1} attempt(s)",
                message="多次重试后仍未完成，已标记为未完成（保留现场，可手动重试）。",
            )
            return

        pptx = _find_pptx(project_path, result.stdout)
        if pptx is None:
            store.update(job_id, status=JobStatus.INTERRUPTED,
                         error="agent finished but no .pptx was found in exports/",
                         message="未找到导出的 PPTX，已标记为未完成。")
            return

        store.update(job_id, pptx_path=pptx, message="正在上传产物到对象存储…")
        _finalize_delivery(job_id, project_path, pptx, job.upload_path)
    except Exception as exc:  # noqa: BLE001
        store.update(job_id, status=JobStatus.INTERRUPTED, error=str(exc),
                     message="生成过程中出错，已标记为未完成。")
    finally:
        heartbeat_stop.set()
        registry.unregister(job_id)


def _run_agent_with_retries(job_id: str, handle: RunHandle, project_path: Path, job):
    """Run the agent up to (1 + agent_retries) times. First run uses the full
    generation prompt for a fresh job, or the resume prompt for a retry; every
    subsequent attempt uses the resume prompt so completed work is preserved."""
    total_runs = 1 + max(0, settings.agent_retries)
    started_resumed = job.project_path is not None and Path(job.project_path).is_dir()
    result = None
    for attempt in range(total_runs):
        if handle.cancelled:
            return result
        use_resume = started_resumed or attempt > 0
        prompt = (
            build_resume_prompt(project_path, job.source_filename, job.canvas_format, job.options)
            if use_resume
            else build_generation_prompt(project_path, job.source_filename, job.canvas_format, job.options)
        )
        store.update(
            job_id, status=JobStatus.GENERATING, attempts=(job.attempts + attempt + 1),
            message=("智能体正在生成幻灯片并导出 PPTX…" if attempt == 0
                     else f"上次中断，正在第 {attempt} 次续跑…"),
        )
        result = run_agent(prompt, handle)

        tail = (result.stdout or "")[-settings.log_tail_chars:]
        store.update(job_id, log_tail=tail)

        if result.cancelled:
            return result
        if not result.timed_out and result.returncode == 0:
            return result  # success
        if attempt < total_runs - 1:
            time.sleep(min(5 * (attempt + 1), 30))  # small backoff before resume
    return result


def _finalize_delivery(job_id: str, project_path: Path, pptx: Path,
                       upload_path: Path | None) -> None:
    """Upload the deck and the extracted speaker-notes .md as two separate public
    objects under the job's R2 prefix, persist both URLs, then delete local
    artifacts. On the deck-upload failure mark INTERRUPTED and keep everything
    for inspection/retry."""
    pptx_ct = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    try:
        r2_key, r2_url = storage.upload_file(job_id, pptx, pptx_ct)
    except Exception as exc:  # noqa: BLE001
        store.update(
            job_id, status=JobStatus.INTERRUPTED, error=f"R2 upload failed: {exc}",
            message="产物上传对象存储失败，已保留本地文件以便排查。",
        )
        return

    store.update(job_id, r2_key=r2_key, r2_url=r2_url)

    # Speaker notes: extract to a single .md and upload as a second object.
    # Best-effort — a notes failure must never fail an otherwise-good job.
    try:
        notes_md = build_speaker_notes_md(project_path, pptx)
        if notes_md is not None and notes_md.is_file():
            notes_key, notes_url = storage.upload_file(job_id, notes_md, "text/markdown; charset=utf-8")
            store.update(job_id, notes_key=notes_key, notes_url=notes_url)
    except Exception:  # noqa: BLE001
        pass

    _cleanup_local(project_path, upload_path)
    store.update(job_id, status=JobStatus.DONE, project_path=None, pptx_path=None,
                 progress=100, message="生成完成。", error=None)


def _handle_cancel(job_id: str, project_path: Path | None) -> None:
    """User-requested stop: remove local artifacts and any partial R2 objects."""
    try:
        storage.delete_prefix(job_id)
    except Exception:
        pass
    _cleanup_local(project_path, None)
    job = store.get(job_id)
    upload = job.upload_path if job else None
    _cleanup_local(None, upload)
    store.update(job_id, status=JobStatus.CANCELLED, project_path=None, pptx_path=None,
                 r2_key=None, r2_url=None, message="任务已按请求取消。")


def _cleanup_local(project_path: Path | None, upload_path: Path | None) -> None:
    """Best-effort removal of the per-job project directory and source upload."""
    if project_path is not None:
        try:
            shutil.rmtree(project_path, ignore_errors=True)
        except Exception:
            pass
    if upload_path is not None:
        try:
            Path(upload_path).unlink(missing_ok=True)
        except Exception:
            pass
