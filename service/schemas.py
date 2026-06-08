"""Pydantic request/response models. These drive the auto-generated Swagger UI."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CONVERTING = "CONVERTING"
    GENERATING = "GENERATING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"      # stopped on user request
    INTERRUPTED = "INTERRUPTED"  # crashed / exhausted retries (未完成)
    EXPIRED = "EXPIRED"          # cleaned by the 3-day reaper


# Statuses that mean "no more work will happen automatically".
TERMINAL_STATUSES = frozenset({
    JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED,
    JobStatus.INTERRUPTED, JobStatus.EXPIRED,
})
# Statuses that mean "should be running or queued".
ACTIVE_STATUSES = frozenset({
    JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CONVERTING, JobStatus.GENERATING,
})


class IconStyle(str, Enum):
    line = "line"
    filled = "filled"
    none = "none"


class FormulaPolicy(str, Enum):
    mixed = "mixed"
    render_all = "render-all"
    text_only = "text-only"


class ImageMode(str, Enum):
    ai = "ai"
    web = "web"
    placeholder = "placeholder"
    none = "none"


class CreateJobResponse(BaseModel):
    job_id: str = Field(..., description="Identifier used to poll status and download the result.")
    status: JobStatus = Field(..., description="Initial job status (PENDING).")
    message: str = Field("", description="Human-readable note.")


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int = Field(0, ge=0, le=100, description="Overall completion percentage (0–100).")
    message: str = ""
    source_filename: str | None = None
    canvas_format: str | None = None
    pptx_filename: str | None = Field(
        None, description="Deprecated alias of download_filename (the packaged .zip name)."
    )
    download_filename: str | None = Field(
        None, description="Filename of the packaged result (.zip), set once status is DONE."
    )
    download_url: str | None = Field(
        None, description="Public R2 URL of the packaged .zip (deck + speaker notes), set once DONE."
    )
    attempts: int = Field(0, description="How many times the agent step has been (re)run.")
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ActionResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str = ""


class ErrorResponse(BaseModel):
    detail: str
