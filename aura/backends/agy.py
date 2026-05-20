"""AgyCLIBackend — calls the antigravity IDE via the `agy` CLI tool.

Authentication: relies on `agy` CLI being authenticated (OAuth).
"""

from __future__ import annotations

import shlex
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aura.backends.cli_base import CLIAgentBackend
from aura.backends.cli_protocol import CLIEventAdapter
from aura.cli_tools import resolve_cli_executable
from aura.client.events import ApiError, Event
from aura.config import ThinkingMode




class AgyCLIBackend(CLIAgentBackend):
    """Agent backend that calls the Antigravity IDE via the `agy` CLI."""

    auth_command = "agy" 

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__(workspace_root=workspace_root)

    def check_auth(self) -> bool:
        """Check if agy CLI is authenticated."""
        path = resolve_cli_executable("agy")
        if path is None:
            return False

        import subprocess
        from aura.config import get_subprocess_kwargs

        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                **get_subprocess_kwargs(),
            )
            return result.returncode == 0
        except Exception:
            return False

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        resolved = resolve_cli_executable("agy")
        if resolved is None:
            yield ApiError(status_code=None, message="agy CLI not found.")
            return

        prompt_text = self._build_prompt(messages, tools)
        
        # We pass the prompt via stdin to avoid command-line length limits.
        # --skip-trust to avoid interactive prompts in headless mode.
        if sys.platform == "win32":
            quoted_resolved = f'"{resolved}"'
        else:
            quoted_resolved = shlex.quote(resolved)

        # --prompt "" ensures non-interactive mode and reads from stdin.
        # --dangerously-skip-permissions ensures it doesn't prompt for permission when executing tools.
        command = f"{quoted_resolved} --prompt \"\" --dangerously-skip-permissions"
        
        adapter = CLIEventAdapter()
        
        result = yield from self._run_cli_agent_command(
            command=command,
            label="Antigravity",
            timeout=120,
            cancel_event=cancel_event,
            input_data=prompt_text,
            adapter=adapter,
        )

        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        if not result.ok:
            yield ApiError(status_code=None, message=f"Antigravity CLI error: {result.stderr or result.stdout}")
            return
