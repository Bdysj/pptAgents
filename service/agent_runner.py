"""Route A: drive Claude Code headless via a CLI subprocess.

We run `claude -p "<prompt>"` with cwd = repo root so Claude Code auto-loads
CLAUDE.md and the ppt-master skill. The relay configuration (ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN and the OpenAI image relay) is injected via the environment.

The agent runs in its own process group (``start_new_session=True``) so the
cancel endpoint can take down the CLI *and* any child processes it spawns via
``os.killpg``. The live ``Popen`` handle is published into the run registry so
cancellation can reach it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .config import settings
from .registry import RunHandle


@dataclass
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool = False


def build_command(prompt: str) -> list[str]:
    cmd = [
        settings.claude_bin,
        "-p", prompt,
        "--permission-mode", settings.claude_permission_mode,
        "--add-dir", str(settings.repo_root),
        "--output-format", "text",
    ]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]
    return cmd


def run_agent(prompt: str, handle: RunHandle | None = None) -> AgentResult:
    """Run the agent to completion (blocking). Intended to be called inside a
    worker thread, not on the event loop.

    If ``handle`` is provided, the live Popen is attached to it so the job can be
    cancelled (SIGTERM to the process group) from another thread.
    """
    cmd = build_command(prompt)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(settings.repo_root),
            env=settings.agent_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group -> killable as a unit
        )
    except FileNotFoundError:
        return AgentResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"Claude Code CLI not found at '{settings.claude_bin}'. "
                "Install it (npm i -g @anthropic-ai/claude-code) or set CLAUDE_CLI_PATH."
            ),
            timed_out=False,
        )

    if handle is not None:
        handle.proc = proc
        # If cancellation was requested before the process came up, honor it now.
        if handle.cancelled:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        stdout, stderr = proc.communicate(timeout=settings.job_timeout)
        cancelled = bool(handle and handle.cancelled)
        return AgentResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
            cancelled=cancelled,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            stdout, stderr = "", ""
        return AgentResult(
            returncode=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
            cancelled=bool(handle and handle.cancelled),
        )
