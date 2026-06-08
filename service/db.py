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

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canvas_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    # Local filesystem bookkeeping (cleared after successful upload + delete).
    upload_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    project_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pptx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pptx_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # R2 delivery (a single packaged .zip containing the deck + speaker notes).
    r2_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    r2_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Misc.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Progress + liveness + retry bookkeeping.
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


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
    """If the existing `jobs` table is missing any expected column, drop it so
    create_all() can rebuild with the full schema. Job records are disposable
    (the deliverable lives in R2), so a one-time rebuild on a schema change is
    acceptable and avoids hand-written ALTERs. No-op once the schema matches."""
    try:
        inspector = inspect(engine)
        if not inspector.has_table(JobRow.__tablename__):
            return
        existing = {c["name"] for c in inspector.get_columns(JobRow.__tablename__)}
        expected = set(JobRow.__table__.columns.keys())
        if not expected.issubset(existing):
            JobRow.__table__.drop(engine, checkfirst=True)
    except Exception:
        # If introspection fails, let create_all proceed; real errors surface later.
        pass


def session_factory() -> sessionmaker:
    if _Session is None:
        init_db()
    assert _Session is not None
    return _Session


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
