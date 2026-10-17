"""Runs the Claude Code CLI headlessly for one commander turn."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from game.claudecommander.config import ClaudeRunConfig, MCP_SERVER_NAME
from game.claudecommander.events import (
    InitEvent,
    OtherEvent,
    ResultEvent,
    StreamEvent,
    parse_line,
    user_message,
)

EventCallback = Callable[[StreamEvent | OtherEvent], None]

_STDERR_TAIL_LINES = 50


class ClaudeNotFoundError(RuntimeError):
    pass


@dataclass
class ClaudeRunResult:
    session_id: Optional[str] = None
    success: bool = False
    final_text: str = ""
    error: Optional[str] = None
    timed_out: bool = False
    cancelled: bool = False
    total_cost_usd: Optional[float] = None
    stderr_tail: list[str] = field(default_factory=list)


def resolve_executable(configured: str) -> str:
    resolved = shutil.which(configured or "claude")
    if resolved is None:
        raise ClaudeNotFoundError(
            f"Could not find the Claude Code CLI ({configured or 'claude'}). Install "
            "Claude Code or set its path in the Claude Commander settings."
        )
    return resolved


@dataclass(frozen=True)
class SessionArgs:
    """Either resumes an existing session or creates one with a known id."""

    session_id: str
    resume: bool

    def to_args(self) -> list[str]:
        if self.resume:
            return ["--resume", self.session_id]
        return ["--session-id", self.session_id]


def build_command(
    executable: str,
    config: ClaudeRunConfig,
    session: SessionArgs,
    mcp_config_path: Path,
    system_prompt_path: Path,
) -> list[str]:
    return [
        executable,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        config.model,
        "--effort",
        config.effort,
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        # No settings, hooks, skills or built-in tools: the commander can only act
        # through its own side's MCP endpoint. This is what enforces fog of war.
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--tools",
        "",
        "--allowedTools",
        f"mcp__{MCP_SERVER_NAME}",
        "--system-prompt-file",
        str(system_prompt_path),
        *session.to_args(),
    ]


def write_mcp_config(path: Path, url: str, token: str) -> None:
    config = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


class ClaudeRun:
    """One CLI invocation: send a single user message, stream events until done."""

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        message: str,
        timeout_seconds: float,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.message = message
        self.timeout_seconds = timeout_seconds
        self.on_event = on_event
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._cancelled = threading.Event()
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def cancel(self) -> None:
        self._cancelled.set()
        if self._process is not None:
            _kill_tree(self._process)

    def run(self) -> ClaudeRunResult:
        result = ClaudeRunResult()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        process = self._process
        assert process.stdin is not None
        assert process.stdout is not None

        stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        stderr_thread.start()

        process.stdin.write((user_message(self.message) + "\n").encode("utf-8"))
        process.stdin.close()

        deadline = time.monotonic() + self.timeout_seconds
        watchdog = threading.Thread(
            target=self._watchdog, args=(deadline, result), daemon=True
        )
        watchdog.start()

        for raw in process.stdout:
            for event in parse_line(raw.decode("utf-8", errors="replace")):
                if isinstance(event, InitEvent):
                    result.session_id = event.session_id
                elif isinstance(event, ResultEvent):
                    result.session_id = event.session_id or result.session_id
                    result.success = not event.is_error
                    result.final_text = event.result
                    result.total_cost_usd = event.total_cost_usd
                    if event.is_error:
                        result.error = "; ".join(event.errors) or event.subtype
                if self.on_event is not None:
                    try:
                        self.on_event(event)
                    except Exception:
                        logging.exception("Commander event callback failed")

        process.wait()
        stderr_thread.join(timeout=1)
        result.stderr_tail = list(self._stderr)
        if self._cancelled.is_set() and not result.timed_out:
            result.cancelled = True
            result.success = False
            result.error = "Planning was cancelled."
        elif result.timed_out:
            result.success = False
            result.error = (
                f"Planning timed out after {self.timeout_seconds / 60:.0f} minutes."
            )
        elif not result.success and result.error is None:
            detail = result.stderr_tail[-1] if result.stderr_tail else ""
            result.error = (
                f"Claude Code exited with code {process.returncode} without a "
                f"result. {detail}".strip()
            )
        return result

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw in self._process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr.append(line)

    def _watchdog(self, deadline: float, result: ClaudeRunResult) -> None:
        assert self._process is not None
        while self._process.poll() is None:
            if self._cancelled.is_set():
                return
            if time.monotonic() >= deadline:
                result.timed_out = True
                _kill_tree(self._process)
                return
            time.sleep(0.5)
