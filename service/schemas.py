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


class CanvasFormat(str, Enum):
    """Canvas size / aspect ratio of the output."""
    ppt169 = "ppt169"
    ppt43 = "ppt43"
    xiaohongshu = "xiaohongshu"
    moments = "moments"
    story = "story"
    wechat = "wechat"
    banner = "banner"
    a4 = "a4"


class StylePreset(str, Enum):
    """Visual + narrative style preset (maps to an executor style + color scheme)."""
    modern = "modern"
    mckinsey = "mckinsey"
    consulting = "consulting"
    tech = "tech"
    academic = "academic"
    government = "government"


class OutputLanguage(str, Enum):
    """Language for BOTH the slides and the speaker notes."""
    auto = "auto"
    zh = "zh"
    zh_tw = "zh-tw"
    en = "en"
    ja = "ja"
    ko = "ko"
    fr = "fr"
    de = "de"
    es = "es"
    ru = "ru"


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
    job_id: str = Field(..., description="任务 ID。")
    status: JobStatus = Field(
        ...,
        description=(
            "任务状态：PENDING(排队中) / CONVERTING(转换素材) / GENERATING(生成中) / "
            "DONE(已完成) / FAILED(失败) / INTERRUPTED(中断未完成，可 retry) / "
            "CANCELLED(已取消) / EXPIRED(超期已清理)。"
        ),
    )
    progress: int = Field(
        0, ge=0, le=100,
        description="整体完成度百分比(0–100)。处理中据此显示进度条；DONE 时为 100。",
    )
    message: str = Field("", description="人类可读的当前状态说明。")
    source_filename: str | None = Field(None, description="原始上传文件名。")
    canvas_format: str | None = Field(None, description="画布格式(如 ppt169)。")
    pptx_filename: str | None = Field(
        None, description="生成的 PPTX 文件名(DONE 后有值)。"
    )
    download_filename: str | None = Field(
        None, description="主产物(PPTX)文件名，等同 pptx_filename。"
    )
    download_url: str | None = Field(
        None,
        description="【DONE 后返回】PPTX 的 Cloudflare 公开可访问 URL，可直接下载/嵌入。",
    )
    notes_url: str | None = Field(
        None,
        description=(
            "【DONE 后返回】演讲稿(speaker notes).md 的 Cloudflare 公开 URL。"
            "若该 deck 无备注则为 null。"
        ),
    )
    attempts: int = Field(0, description="智能体生成步骤已(重)跑的次数。")
    error: str | None = Field(None, description="失败/中断时的错误摘要。")
    created_at: datetime
    updated_at: datetime


class ActionResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str = ""


class ErrorResponse(BaseModel):
    detail: str
