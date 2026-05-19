"""CodexBackend — calls Codex via `codex exec`.

Authentication: relies on `codex login`.
"""

from __future__ import annotations

import logging
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
from aura.sandbox import SandboxExecutor

logger = logging.getLogger(__name__)


class CodexBackend(CLIAgentBackend):
    """Agent backend that calls Codex via the `codex` CLI."""

    auth_command = "codex login"

    # --- Additional auth command variants ---
    device_auth_command = "codex login --device-auth"
    api_key_auth_command = "codex login --with-api-key"  # Takes stdin; for manual use only

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__(workspace_root=workspace_root)

    def check_auth(self) -> bool:
        """Check if codex is authenticated."""
        path = resolve_cli_executable("codex")
        if path is None:
            return False

        import subprocess
        from aura.config import get_subprocess_kwargs

        try:
            result = subprocess.run(
                [path, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **get_subprocess_kwargs(),
            )
            return result.returncode == 0 and "Logged in" in result.stdout
        except Exception:
            return False

    def run_device_auth(self) -> bool:
        """Launch device-code auth flow and poll for completion.

        Returns:
            True if authentication succeeded within the timeout, False otherwise.
        """
        launched = SandboxExecutor._launch_interactive_terminal(
            command=self.device_auth_command,
            workspace_root=self._workspace_root,
        )

        if not launched:
            logger.warning("Failed to launch device auth terminal for codex.")
            return False

        import time

        deadline = time.monotonic() + self.auth_timeout_seconds
        while time.monotonic() < deadline:
            try:
                if self.check_auth():
                    logger.info("Codex device auth succeeded.")
                    return True
            except Exception as exc:
                logger.exception("check_auth() raised during device auth polling: %s", exc)
                return False
            time.sleep(2)

        logger.warning("Codex device auth timed out after %d seconds.", self.auth_timeout_seconds)
        return False

    @staticmethod
    def get_manual_auth_instructions() -> str:
        """Return human-readable fallback auth instructions."""
        return (
            "Codex login did not complete inside Aura. You can authenticate manually "
            "by opening a terminal and running:\n  codex login\nor:\n  codex login --device-auth\n"
            "Then return to Aura and click Recheck Status."
        )

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

        resolved = resolve_cli_executable("codex")
        if resolved is None:
            yield ApiError(status_code=None, message="codex CLI not found.")
            return

        prompt_text = self._build_prompt(messages, tools)
        
        if sys.platform == "win32":
            quoted_resolved = f'"{resolved}"'
        else:
            quoted_resolved = shlex.quote(resolved)

        # Use exec for non-interactive output
        command = f"{quoted_resolved} exec {shlex.quote(prompt_text)}"
        
        adapter = CLIEventAdapter()

        result = yield from self._run_cli_agent_command(
            command=command,
            label="Codex",
            timeout=120,
            cancel_event=cancel_event,
            adapter=adapter,
        )

        if cancel_event and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        if not result.ok:
            yield ApiError(status_code=None, message=f"Codex error: {result.stderr or result.stdout}")
            return
