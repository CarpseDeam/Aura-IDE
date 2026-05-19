"""Base class for CLI-based agent backends that require device/CLI auth."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from pathlib import Path
from collections.abc import Generator
from typing import Any

from aura.backends.base import AgentBackend
from aura.client.events import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    Event,
)
from aura.sandbox import SandboxExecutor, SandboxResult
from aura.backends.cli_protocol import CLIEventAdapter

logger = logging.getLogger(__name__)


class CLIAgentBackend(AgentBackend):
    """Base for backends that shell out to a CLI tool (gcloud, gh, codex, etc.).

    Subclasses must provide:
      - auth_command: the shell command to run for interactive auth
        (e.g., "gcloud auth application-default login")
      - A check_auth() implementation that probes whether auth already exists
        (e.g., runs a credential check command or looks for a credential file)

    The default implementations of check_auth() and run_cli_auth() in the
    parent AgentBackend class assume API-key-based auth is always ready;
    CLI backends override them here.
    """

    # Override in subclasses — the shell command for interactive auth.
    auth_command: str | None = None

    # Maximum seconds to wait for auth to complete after launching terminal.
    auth_timeout_seconds: int = 120

    def __init__(self, workspace_root: Path | None = None) -> None:
        """Initialise the CLI backend.

        Args:
            workspace_root: Working directory for subprocess execution.
                Defaults to ``Path.cwd()``.
        """
        self._workspace_root = workspace_root or Path.cwd()

    def check_auth(self) -> bool:
        """Probe whether the CLI tool has valid credentials.

        Subclasses must override this to run the actual credential check.

        Returns:
            True if credentials are valid, False otherwise.
        """
        return False

    def run_cli_auth(self) -> bool:
        """Launch the auth_command in an interactive terminal, then poll for auth.

        Launches the terminal detached (fire-and-forget via
        :meth:`SandboxExecutor._launch_interactive_terminal`), then polls
        :meth:`check_auth` every 2 seconds until the timeout expires.

        Returns:
            True if authentication succeeded (check_auth returns True within
            the timeout), False otherwise.
        """
        if not self.auth_command:
            return True  # No auth command configured; assume already authed

        # Launch the terminal (non-blocking, fire-and-forget)
        launched = SandboxExecutor._launch_interactive_terminal(
            command=self.auth_command,
            workspace_root=self._workspace_root,
        )

        if not launched:
            logger.warning(
                "Failed to launch interactive terminal for auth command: %s",
                self.auth_command,
            )
            return False

        # Poll check_auth() until success or timeout
        import time

        deadline = time.monotonic() + self.auth_timeout_seconds
        while time.monotonic() < deadline:
            try:
                if self.check_auth():
                    logger.info("Auth succeeded for command: %s", self.auth_command)
                    return True
            except Exception as exc:
                logger.exception("check_auth() raised during polling: %s", exc)
                return False
            time.sleep(2)

        logger.warning(
            "Auth timed out after %d seconds for command: %s",
            self.auth_timeout_seconds,
            self.auth_command,
        )
        return False

    def _run_cli_agent_command(
        self,
        *,
        command: str,
        label: str,
        timeout: int = 120,
        cancel_event: threading.Event | None = None,
        input_data: str | None = None,
        adapter: CLIEventAdapter | None = None,
    ) -> Generator[Event, None, SandboxResult]:
        """Run a CLI agent command while yielding live process output events."""
        process_id = f"cli-{uuid.uuid4().hex}"
        events: queue.Queue[tuple[str, str | SandboxResult]] = queue.Queue()

        yield AgentProcessStarted(
            process_id=process_id,
            label=label,
            command=command,
        )

        def on_output(text: str) -> None:
            events.put(("output", text))

        def run_command() -> None:
            try:
                sandbox = SandboxExecutor(mode="host", workspace_root=self._workspace_root)
                result = sandbox.run_terminal_command(
                    command=command,
                    timeout=timeout,
                    cancel_event=cancel_event,
                    on_output=on_output,
                    input_data=input_data,
                )
            except Exception as exc:
                result = SandboxResult(
                    ok=False,
                    stdout="",
                    stderr=f"{type(exc).__name__}: {exc}",
                    exit_code=-1,
                )
            events.put(("result", result))

        thread = threading.Thread(
            target=run_command,
            name=f"Aura {label} CLI process",
            daemon=True,
        )
        thread.start()

        result: SandboxResult | None = None
        while result is None:
            kind, payload = events.get()
            if kind == "output":
                chunk = str(payload)
                yield AgentProcessOutput(process_id=process_id, text=chunk)
                if adapter:
                    yield from adapter.feed(chunk)
            else:
                result = payload if isinstance(payload, SandboxResult) else SandboxResult(
                    ok=False,
                    stdout="",
                    stderr="CLI process did not return a SandboxResult.",
                    exit_code=-1,
                )

        thread.join(timeout=0)
        
        if adapter:
            yield from adapter.finish(result.exit_code, result.stdout, result.stderr)

        yield AgentProcessFinished(process_id=process_id, exit_code=result.exit_code)
        return result

    def _build_prompt(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
        import json
        parts = []
        
        if tools:
            tools_json = json.dumps(tools, indent=2)
            protocol = (
                "You have access to the following tools:\n"
                f"{tools_json}\n\n"
                "To call a tool, you MUST output a JSON block exactly like this, on its own line:\n"
                'AURA_EVENT {"type": "tool_call_start", "id": "call-1", "name": "tool_name", "index": 0}\n'
                'AURA_EVENT {"type": "tool_call_args", "index": 0, "args_chunk": "{\\"arg1\\": \\"value\\"}"}\n'
                'AURA_EVENT {"type": "tool_call_end", "index": 0}\n'
                "Do NOT output conversational prose before the tool call if the tool call is your primary action.\n"
                "After you output the tool call, STOP. The system will provide the result in a TOOL message."
            )
            parts.append(f"SYSTEM: {protocol}")

        for m in messages:
            role = m.get("role", "").upper()
            content = m.get("content", "")
            
            msg_parts = []
            if content:
                msg_parts.append(content)
                
            if "tool_calls" in m:
                for tc in m["tool_calls"]:
                    tid = tc.get("id", "")
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    msg_parts.append(f'AURA_EVENT {{"type": "tool_call_start", "id": "{tid}", "name": "{name}", "index": 0}}')
                    msg_parts.append(f'AURA_EVENT {{"type": "tool_call_args", "index": 0, "args_chunk": {json.dumps(args)}}}')
                    msg_parts.append(f'AURA_EVENT {{"type": "tool_call_end", "index": 0}}')
            
            if role == "TOOL":
                # The tool message might have tool_call_id, but the LLM just needs the result.
                tid = m.get("tool_call_id", "")
                msg_parts.insert(0, f"[Result for tool call {tid}]")

            if msg_parts:
                parts.append(f"{role}:\n" + "\n".join(msg_parts))

        return "\n\n".join(parts)
