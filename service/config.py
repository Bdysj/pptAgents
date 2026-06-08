"""Runtime configuration for the ppt-master FastAPI service.

All values come from environment variables (optionally loaded from a .env at
the repo root by your process manager / docker). Nothing here is secret by
default; secrets (API keys / tokens) must be supplied via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = parent of the `service/` package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "ppt-master"
PROJECT_MANAGER = SKILL_DIR / "scripts" / "project_manager.py"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency).

    Loads KEY=VALUE lines from `path` into os.environ WITHOUT overriding any
    variable already present in the process environment (process env wins,
    matching docker-compose and the skill's own .env precedence).
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip inline comments (a '#' preceded by whitespace) on unquoted values,
        # so lines like `KEY=val  # note` load `val`, not `val  # note`.
        if value and value[0] not in "\"'":
            for marker in (" #", "\t#"):
                idx = value.find(marker)
                if idx != -1:
                    value = value[:idx]
            value = value.strip()
        value = value.strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load repo-root .env early so settings below see it (process env still wins).
_load_dotenv(REPO_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """Process-wide settings, read once at import time."""

    # ── Paths ─────────────────────────────────────────────────────────
    repo_root: Path = REPO_ROOT
    skill_dir: Path = SKILL_DIR
    project_manager: Path = PROJECT_MANAGER
    projects_dir: Path = Path(os.environ.get("PPT_PROJECTS_DIR", str(REPO_ROOT / "projects")))
    uploads_dir: Path = Path(os.environ.get("PPT_UPLOADS_DIR", str(REPO_ROOT / "uploads")))

    # ── Python / Claude Code executables ──────────────────────────────
    python_bin: str = os.environ.get("PPT_PYTHON_BIN", "python3")
    claude_bin: str = os.environ.get("CLAUDE_CLI_PATH", "claude")
    claude_model: str | None = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8") or None
    # Permission mode for headless runs. `bypassPermissions` is required so the
    # agent can run bash + write files without interactive approval.
    claude_permission_mode: str = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")

    # ── Brain LLM relay (Claude protocol) ─────────────────────────────
    anthropic_base_url: str | None = os.environ.get("ANTHROPIC_BASE_URL", "https://api.vectorengine.ai") or None
    anthropic_auth_token: str | None = os.environ.get("ANTHROPIC_AUTH_TOKEN") or None
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY") or None

    # ── Image model relay (OpenAI protocol, gpt-image-2) ──────────────
    # These are consumed by skills/.../image_backends/backend_openai.py via the
    # agent subprocess environment. Process env takes precedence over any .env.
    image_backend: str = os.environ.get("IMAGE_BACKEND", "openai")
    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY") or None
    openai_base_url: str | None = os.environ.get("OPENAI_BASE_URL", "https://api.vectorengine.ai/v1") or None
    openai_model: str = os.environ.get("OPENAI_MODEL", "gpt-image-2")

    # ── Job execution ─────────────────────────────────────────────────
    job_timeout: int = _int("PPT_JOB_TIMEOUT", 3600)          # seconds
    max_concurrency: int = _int("PPT_MAX_CONCURRENCY", 20)
    default_format: str = os.environ.get("PPT_DEFAULT_FORMAT", "ppt169")
    log_tail_chars: int = _int("PPT_LOG_TAIL_CHARS", 8000)
    # Resume-aware retries of the agent step (per job) before giving up.
    agent_retries: int = _int("PPT_AGENT_RETRIES", 2)
    # Heartbeat cadence written while the agent runs (zombie detection).
    heartbeat_seconds: int = _int("PPT_HEARTBEAT_SECONDS", 30)
    # Reaper: unfinished jobs older than this are cleaned up (R2 + local).
    job_expire_days: int = _int("PPT_JOB_EXPIRE_DAYS", 3)
    reaper_interval_seconds: int = _int("PPT_REAPER_INTERVAL_SECONDS", 3600)
    # Backpressure: max PENDING jobs before POST returns 429. 0 = unbounded.
    queue_max_pending: int = _int("PPT_QUEUE_MAX_PENDING", 0)

    # ── Persistence (MySQL via SQLAlchemy) ────────────────────────────
    # SQLAlchemy URL, e.g.
    #   mysql+pymysql://user:pass@host:3306/pptMaster?charset=utf8mb4
    database_url: str | None = os.environ.get("DATABASE_URL") or None

    # ── Cloudflare R2 (S3-compatible object storage) ──────────────────
    r2_account_id: str | None = os.environ.get("R2_ACCOUNT_ID") or None
    r2_access_key_id: str | None = os.environ.get("R2_ACCESS_KEY_ID") or None
    r2_secret_access_key: str | None = os.environ.get("R2_SECRET_ACCESS_KEY") or None
    r2_bucket: str | None = os.environ.get("R2_BUCKET_NAME") or None
    r2_public_domain: str | None = os.environ.get("R2_PUBLIC_DOMAIN") or None
    r2_use_https: bool = _bool("R2_USE_HTTPS", True)
    r2_key_prefix: str = (os.environ.get("R2_KEY_PREFIX", "presentations") or "presentations").strip("/")

    @property
    def r2_enabled(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket
            and self.r2_public_domain
        )

    @property
    def r2_endpoint(self) -> str | None:
        if not self.r2_account_id:
            return None
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    def r2_public_url(self, key: str) -> str:
        scheme = "https" if self.r2_use_https else "http"
        return f"{scheme}://{self.r2_public_domain}/{key.lstrip('/')}"

    def agent_env(self) -> dict[str, str]:
        """Environment to inject into the Claude Code subprocess.

        Inherits the current process environment, then overlays the relay
        configuration for both the brain LLM and the image model.
        """
        env = dict(os.environ)
        if self.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        if self.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_auth_token
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key

        # Image generation relay (gpt-image-2).
        env["IMAGE_BACKEND"] = self.image_backend
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        if self.openai_base_url:
            env["OPENAI_BASE_URL"] = self.openai_base_url
        env["OPENAI_MODEL"] = self.openai_model
        return env

    def ensure_dirs(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
