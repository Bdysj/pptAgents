"""Durable persistence for jobs via SQLAlchemy (MySQL by default).

The job store keeps only lightweight metadata (a few hundred bytes per job):
status, the source filename, and — once finished — the public R2 URL of the
delivered .pptx. The file itself lives in Cloudflare R2, not in the database.

Why a database at all: after a job finishes we upload the .pptx to R2 and delete
the local project directory. The status/download endpoints must still answer for
that job_id across restarts, so the job → R2-URL mapping has to survive both the
folder deletion and container/process restarts.

The connection string comes from DATABASE_URL, e.g.
    mysql+pymysql://user:pass@host:3306/pptMaster?charset=utf8mb4
Swapping to Postgres/SQLite later is just a different URL; the model is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(
        String(32), primary_key=True,
        comment="任务ID(主键,12位十六进制)")
    source_filename: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="原始上传的源文件名")
    canvas_format: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="画布格式: ppt169/ppt43/xiaohongshu/moments/story/wechat/banner/a4")
    status: Mapped[str] = mapped_column(
        String(16), index=True,
        comment="任务状态: PENDING/CONVERTING/GENERATING/DONE/FAILED/CANCELLED/INTERRUPTED/EXPIRED")
    fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="幂等指纹=sha256(文件内容+生成参数); 相同提交复用任务, 防重放")
    message: Mapped[str] = mapped_column(
        Text, default="",
        comment="当前状态的人类可读说明")
    upload_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="源文件在服务器的暂存路径(任务成功完成后清理)")
    project_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="生成项目目录路径(成功后清理; 中断时保留以供断点续跑)")
    pptx_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="本地生成的PPTX路径(上传R2后清空)")
    pptx_filename: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="生成的PPTX文件名")
    r2_key: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="PPTX在R2的对象Key(前缀 presentations/<job_id>/)")
    r2_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="PPTX的Cloudflare公开可访问URL")
    notes_key: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="演讲稿md在R2的对象Key")
    notes_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="演讲稿md的Cloudflare公开URL(无备注则为空)")
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="失败/中断时的错误摘要")
    options: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="生成参数JSON(style/page_count/image_mode/formula_policy/output_language等)")
    log_tail: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="智能体输出日志尾部(排查用)")
    progress: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="完成进度百分比(0-100), 单调递增")
    attempts: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="智能体生成步骤已运行次数(含自动重试)")
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="运行期心跳时间(UTC), 用于僵尸/崩溃检测")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        comment="任务创建时间(UTC)")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        comment="最后更新时间(UTC)")


_engine = None
_Session: sessionmaker | None = None


def _ensure_database_exists(url) -> None:
    """Mirror JDBC's createDatabaseIfNotExist: connect to the server without a
    database selected and CREATE DATABASE IF NOT EXISTS. Best-effort; ignored
    for backends/permissions where it does not apply."""
    db_name = url.database
    if not db_name:
        return
    server_url = url.set(database=None)
    try:
        server_engine = create_engine(server_url, pool_pre_ping=True)
        with server_engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            conn.commit()
        server_engine.dispose()
    except Exception:
        # If we lack CREATE privileges or the backend differs, assume the DB
        # already exists and let the real connection surface any real error.
        pass


def init_db() -> None:
    """Create the engine + tables. Call once at startup."""
    global _engine, _Session
    if _engine is not None:
        return
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Configure it in .env, e.g. "
            "mysql+pymysql://user:pass@host:3306/pptMaster?charset=utf8mb4"
        )
    url = make_url(settings.database_url)
    if url.get_backend_name().startswith("mysql"):
        _ensure_database_exists(url)
    _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)
    _reconcile_schema(_engine)
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _reconcile_schema(engine) -> None:
    """Keep the existing `jobs` table in sync with the model (MySQL only):
      1. ADD any missing columns in place (additive — never drops data).
      2. Sync every column's COMMENT to match the model, restating each column's
         *current* type + nullability (read from information_schema) so only the
         comment changes — no type/constraint side effects.
    Safe to run while jobs are live; no-op once already in sync."""
    try:
        if not engine.dialect.name.startswith("mysql"):
            return
        inspector = inspect(engine)
        if not inspector.has_table(JobRow.__tablename__):
            return
        table = JobRow.__tablename__
        existing = {c["name"] for c in inspector.get_columns(table)}

        with engine.begin() as conn:
            # 1) Additive: add missing columns (with comment + correct nullability).
            for col in JobRow.__table__.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                null_sql = "NULL" if col.nullable else "NOT NULL"
                comment_sql = _comment_clause(col.comment)
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN `{col.name}` "
                    f"{col_type} {null_sql}{comment_sql}"
                ))

            # 2) Sync comments on all model columns, preserving live definition.
            for col in JobRow.__table__.columns:
                if not col.comment:
                    continue
                info = conn.execute(text(
                    "SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                    "AND COLUMN_NAME = :c"
                ), {"t": table, "c": col.name}).first()
                if info is None:
                    continue
                column_type, is_nullable = info[0], info[1]
                null_sql = "NULL" if is_nullable == "YES" else "NOT NULL"
                comment_sql = _comment_clause(col.comment)
                conn.execute(text(
                    f"ALTER TABLE {table} MODIFY COLUMN `{col.name}` "
                    f"{column_type} {null_sql}{comment_sql}"
                ))
    except Exception:
        # If introspection/ALTER fails, let create_all proceed; real errors surface later.
        pass


def _comment_clause(comment: str | None) -> str:
    if not comment:
        return ""
    escaped = comment.replace("'", "''")
    return f" COMMENT '{escaped}'"


def session_factory() -> sessionmaker:
    if _Session is None:
        init_db()
    assert _Session is not None
    return _Session


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
