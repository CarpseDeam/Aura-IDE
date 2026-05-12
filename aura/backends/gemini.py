"""GeminiCLIBackend — calls Google via the `gemini` CLI tool.

Authentication: relies on `gemini` CLI being authenticated (OAuth).
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


class GeminiCLIBackend(CLIAgentBackend):
    """Agent backend that calls Google via the `gemini` CLI."""

    # gemini CLI requires 'login' command for OAuth flow
    auth_command = "gemini login" 

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__(workspace_root=workspace_root)

    def check_auth(self) -> bool:
        """Check if gemini CLI is likely authenticated."""
        path = shutil.which("gemini")
        if path is None:
            return False

        # Simple check: can we get a version?
        import subprocess
        from aura.config import get_subprocess_kwargs

        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
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

        if shutil.which("gemini") is None:
            yield ApiError(status_code=None, message="gemini CLI not found.")
            return

        prompt_text = self._build_prompt(messages)
        
        # We pass the prompt via stdin to avoid command-line length limits.
        # --skip-trust to avoid interactive prompts in headless mode.
        command = "gemini --skip-trust"
        
        sandbox = SandboxExecutor(mode="host", workspace_root=self._workspace_root)
        result = sandbox.run_terminal_command(
            command=command,
            timeout=120,
            cancel_event=cancel_event,
            input_data=prompt_text,
        )

        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        if not result.ok:
            yield ApiError(status_code=None, message=f"Google CLI error: {result.stderr or result.stdout}")
            return

        output_text = result.stdout.strip()
        yield ContentDelta(text=output_text)
        yield Done(finish_reason="stop", full_message={"role": "assistant", "content": output_text})

    def _build_prompt(self, messages: list[dict[str, Any]]) -> str:
        parts = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role and content and isinstance(content, str):
                parts.append(f"{role.upper()}: {content}")
        return "\n\n".join(parts)
