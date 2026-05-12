"""GeminiCLIAgentBackend — calls Gemini via `gcloud ai models predict`.

Authentication: relies entirely on the user having previously run
`gcloud auth login` and `gcloud config set project PROJECT_ID`.
Aura does not manage gcloud credentials.
"""

from __future__ import annotations

import json
import shlex
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aura.backends.base import AgentBackend
from aura.client.events import ApiError, ContentDelta, Done, Event
from aura.config import ThinkingMode
from aura.sandbox import SandboxExecutor


class GeminiCLIAgentBackend(AgentBackend):
    """Agent backend that calls Gemini via `gcloud ai models predict`.

    This backend shells out to the Google Cloud CLI, so the user must have
    authenticated via ``gcloud auth login`` and set a project via
    ``gcloud config set project PROJECT_ID`` before use.

    No API keys are managed or stored by Aura for this backend.
    """

    def __init__(
        self,
        region: str = "us-central1",
        workspace_root: Path | None = None,
    ) -> None:
        """Initialise the Gemini CLI backend.

        Args:
            region: The Google Cloud region for Vertex AI (default
                ``us-central1``).
            workspace_root: Working directory for sandbox execution.
                Defaults to ``Path.cwd()``.
        """
        self._region = region
        self._workspace_root = workspace_root or Path.cwd()

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        """Stream a Gemini response via the gcloud CLI.

        Args:
            messages: The conversation history in API format.
            tools: Tool definitions (ignored — gcloud CLI does not support
                function calling).
            model: Vertex AI model name, e.g. ``gemini-2.0-flash-001``.
            thinking: Thinking mode (ignored — not supported via gcloud CLI).
            cancel_event: Optional event — when set, stops immediately.
            temperature: Sampling temperature (0.0–2.0).

        Yields:
            ContentDelta, Done on success; ApiError on failure.
        """
        # ------------------------------------------------------------------
        # Early cancellation check
        # ------------------------------------------------------------------
        if cancel_event is not None and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        # ------------------------------------------------------------------
        # Validate inputs
        # ------------------------------------------------------------------
        if not messages:
            yield ApiError(status_code=None, message="No messages provided.")
            return

        # ------------------------------------------------------------------
        # Check gcloud is installed
        # ------------------------------------------------------------------
        if shutil.which("gcloud") is None:
            yield ApiError(
                status_code=None,
                message=(
                    "gcloud CLI not found. Please install the Google Cloud SDK "
                    "and run 'gcloud auth login'."
                ),
            )
            return

        # ------------------------------------------------------------------
        # Build prompt text from messages
        # ------------------------------------------------------------------
        prompt_text = self._build_prompt(messages)

        # ------------------------------------------------------------------
        # Construct the gcloud command
        # ------------------------------------------------------------------
        request_json = json.dumps(
            {
                "instances": [{"prompt": prompt_text}],
                "parameters": {
                    "temperature": temperature,
                    "maxOutputTokens": 8192,
                },
            },
            ensure_ascii=False,
        )

        command = (
            f"gcloud ai models predict {shlex.quote(model)} "
            f"--region={shlex.quote(self._region)} "
            f"--format=json "
            f"--content={shlex.quote(request_json)}"
        )

        # ------------------------------------------------------------------
        # Run via SandboxExecutor
        # ------------------------------------------------------------------
        sandbox = SandboxExecutor(
            mode="host",
            workspace_root=self._workspace_root,
            network_enabled=True,
        )

        result = sandbox.run_terminal_command(
            command=command,
            timeout=120,
            cancel_event=cancel_event,
        )

        # ------------------------------------------------------------------
        # Post-execution cancellation check
        # ------------------------------------------------------------------
        if cancel_event is not None and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        # ------------------------------------------------------------------
        # Handle sandbox / command failure
        # ------------------------------------------------------------------
        if not result.ok:
            yield ApiError(
                status_code=None,
                message=(
                    f"gcloud error (exit {result.exit_code}): "
                    f"{result.stderr or result.stdout}"
                ),
            )
            return

        if not result.stdout.strip():
            yield ApiError(
                status_code=None,
                message="gcloud returned empty output.",
            )
            return

        # ------------------------------------------------------------------
        # Parse JSON output
        # ------------------------------------------------------------------
        output_text = self._parse_output(result.stdout)

        # ------------------------------------------------------------------
        # Yield events
        # ------------------------------------------------------------------
        yield ContentDelta(text=output_text)
        full_message: dict[str, Any] = {
            "role": "assistant",
            "content": output_text,
        }
        yield Done(finish_reason="stop", full_message=full_message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(messages: list[dict[str, Any]]) -> str:
        """Flatten the message list into a single prompt string.

        Args:
            messages: Conversation history in API format.

        Returns:
            A single text string suitable for the Vertex AI prompt field.
        """
        parts: list[str] = []
        system_prefix = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            # Skip tool-call and tool-result messages
            if role == "tool":
                continue
            if role == "assistant" and not content:
                # Assistant message with only tool_calls — skip
                continue

            # Extract text from multimodal content (list of parts)
            if isinstance(content, list):
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts)

            if not content or not isinstance(content, str):
                continue

            if role == "system":
                system_prefix = f"System: {content}\n\n"
            else:
                parts.append(f"{role}: {content}")

        prompt = system_prefix + "\n".join(parts)

        # Truncate if too long (keep system message + last N chars)
        if len(prompt) > 8000:
            tail_len = 8000 - len(system_prefix) - 3  # 3 for "..."
            if tail_len > 0:
                prompt = system_prefix + "..." + prompt[-tail_len:]
            else:
                prompt = prompt[-8000:]

        return prompt

    @staticmethod
    def _parse_output(stdout: str) -> str:
        """Extract the response text from gcloud JSON output.

        Tries multiple parsing strategies to handle different Vertex AI
        response shapes. Falls back to raw stdout on failure.

        Args:
            stdout: Raw stdout from the gcloud command.

        Returns:
            Extracted text content.
        """
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout.strip()

        # Strategy 1: list of predictions
        if isinstance(data, list):
            if not data:
                return stdout.strip()
            predictions = data[0].get("predictions", [])
        elif isinstance(data, dict):
            predictions = data.get("predictions", [])
        else:
            return stdout.strip()

        if not predictions:
            return stdout.strip()

        first = predictions[0]

        # Strategy 2: predictions is a list of strings
        if isinstance(first, str):
            return first

        # Strategy 3: predictions is a list of dicts
        if isinstance(first, dict):
            # Look for candidates[0]["content"]
            candidates = first.get("candidates")
            if isinstance(candidates, list) and candidates:
                candidate = candidates[0]
                if isinstance(candidate, dict):
                    content = candidate.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, dict):
                        text = content.get("text") or content.get("parts", [{}])[0].get("text", "")
                        if text:
                            return text

            # Direct "content" key
            content = first.get("content")
            if isinstance(content, str):
                return content

            # "text" key
            text = first.get("text")
            if isinstance(text, str):
                return text

        # Fallback: return raw stdout
        return stdout.strip()
