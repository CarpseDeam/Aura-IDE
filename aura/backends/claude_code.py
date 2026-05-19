"""ClaudeCodeBackend — calls Claude via `claude -p`.

Authentication: relies on `claude auth login`.
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


class ClaudeCodeBackend(CLIAgentBackend):
    """Agent backend that calls Claude via the `claude` CLI."""

    auth_command = "claude auth login"

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__(workspace_root=workspace_root)

    def check_auth(self) -> bool:
        """Check if claude is authenticated."""
        path = resolve_cli_executable("claude")
        if path is None:
            return False

        import subprocess
        import json
        from aura.config import get_subprocess_kwargs

        try:
            result = subprocess.run(
                [path, "auth", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **get_subprocess_kwargs(),
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
            return data.get("loggedIn") is True
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

        resolved = resolve_cli_executable("claude")
        if resolved is None:
            yield ApiError(status_code=None, message="claude CLI not found.")
            return

        prompt_text = self._build_prompt(messages, tools)
        
        if sys.platform == "win32":
            quoted_resolved = f'"{resolved}"'
        else:
            quoted_resolved = shlex.quote(resolved)

        # Use --bare to skip hooks/CLAUDE.md for speed and predictability
        command = f"{quoted_resolved} -p {shlex.quote(prompt_text)} --bare"
        
        adapter = CLIEventAdapter()

        result = yield from self._run_cli_agent_command(
            command=command,
            label="Claude",
            timeout=120,
            cancel_event=cancel_event,
            adapter=adapter,
        )

        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        if not result.ok:
            yield ApiError(status_code=None, message=f"Claude error: {result.stderr or result.stdout}")
            return

    def _build_prompt(self, messages: list[dict[str, Any]]) -> str:
        # Simple flattening for now
        parts = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role and content and isinstance(content, str):
                parts.append(f"{role.upper()}: {content}")
        return "\n\n".join(parts)
