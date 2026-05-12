"""CodexBackend — calls Codex via `codex exec`.

Authentication: relies on `codex login`.
"""

from __future__ import annotations

import shlex
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aura.backends.cli_base import CLIAgentBackend
from aura.client.events import ApiError, ContentDelta, Done, Event
from aura.config import ThinkingMode
from aura.sandbox import SandboxExecutor


class CodexBackend(CLIAgentBackend):
    """Agent backend that calls Codex via the `codex` CLI."""

    auth_command = "codex login"

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__(workspace_root=workspace_root)

    def check_auth(self) -> bool:
        """Check if codex is authenticated."""
        path = shutil.which("codex")
        if path is None:
            return False

        import subprocess
        from aura.config import get_subprocess_kwargs

        try:
            result = subprocess.run(
                [path, "login", "status"],
                capture_output=True,
                text=True,
                timeout=10,
                **get_subprocess_kwargs(),
            )
            return result.returncode == 0 and "Logged in" in result.stdout
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

        if shutil.which("codex") is None:
            yield ApiError(status_code=None, message="codex CLI not found.")
            return

        prompt_text = self._build_prompt(messages)
        
        # Use exec for non-interactive output
        command = f"codex exec {shlex.quote(prompt_text)}"
        
        sandbox = SandboxExecutor(mode="host", workspace_root=self._workspace_root)
        result = sandbox.run_terminal_command(command=command, timeout=120, cancel_event=cancel_event)

        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        if not result.ok:
            yield ApiError(status_code=None, message=f"Codex error: {result.stderr or result.stdout}")
            return

        output_text = result.stdout.strip()
        yield ContentDelta(text=output_text)
        yield Done(finish_reason="stop", full_message={"role": "assistant", "content": output_text})

    def _build_prompt(self, messages: list[dict[str, Any]]) -> str:
        # Simple flattening for now
        parts = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role and content and isinstance(content, str):
                parts.append(f"{role.upper()}: {content}")
        return "\n\n".join(parts)
