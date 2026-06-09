from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.backends.api import APIAgentBackend
from aura.client.events import ApiError, ContentDelta, Done
from aura.config import ThinkingMode
from aura.drones.build_spec import DroneBuildBrief

# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DroneWorkshopResponse:
    kind: str  # "question", "brief", "error"
    message: str = ""
    brief: DroneBuildBrief | None = None
    raw_text: str = ""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def extract_json_object(text: str) -> dict[str, object] | None:
    """Try to extract a JSON object from *text*.

    Attempts, in order:
    1. Direct ``json.loads`` on the trimmed string.
    2. First JSON fenced code block (`` ```json ... ``` ``).
    3. First ``{...}`` pair found by scanning for braces.
    Returns ``None`` when all attempts fail.
    """
    # 1. Direct parse
    stripped = text.strip()
    if stripped:
        try:
            result = json.loads(stripped)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # 2. Fenced code block  ```json ... ```  or  ``` ... ```
    m = re.search(
        r"```(?:json)?\s*\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if m:
        try:
            result = json.loads(m.group(1).strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # 3. First { ... } pair
    first = text.find("{")
    if first != -1:
        last = text.rfind("}")
        if last > first:
            try:
                result = json.loads(text[first : last + 1])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    return None


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_workshop_response(text: str) -> DroneWorkshopResponse:
    """Parse the LLM's raw output into a ``DroneWorkshopResponse``."""
    try:
        obj = extract_json_object(text)
    except Exception as exc:
        return DroneWorkshopResponse(
            kind="error",
            message=str(exc),
            raw_text=text,
        )

    if obj is None:
        return DroneWorkshopResponse(
            kind="error",
            message="Could not parse a valid JSON object from the response.",
            raw_text=text,
        )

    try:
        resp_type = obj.get("type")
        if not resp_type or not isinstance(resp_type, str):
            return DroneWorkshopResponse(
                kind="error",
                message="Response missing required 'type' field.",
                raw_text=text,
            )

        if resp_type == "question":
            return DroneWorkshopResponse(
                kind="question",
                message=str(obj.get("message", "")),
                raw_text=text,
            )

        if resp_type == "brief":
            brief = DroneBuildBrief(
                response_type="brief",
                message=str(obj.get("message", "")),
                ready_to_build=bool(obj.get("ready_to_build", False)),
                build_brief=str(obj.get("build_brief", "")),
            )
            validation_errors = brief.validate()
            if validation_errors:
                return DroneWorkshopResponse(
                    kind="error",
                    message="; ".join(validation_errors),
                    raw_text=text,
                )
            return DroneWorkshopResponse(
                kind="brief",
                message=str(obj.get("message", "")),
                brief=brief,
                raw_text=text,
            )

        return DroneWorkshopResponse(
            kind="error",
            message=f"Unknown response type '{resp_type}'.",
            raw_text=text,
        )
    except Exception as exc:
        return DroneWorkshopResponse(
            kind="error",
            message=str(exc),
            raw_text=text,
        )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

DRONE_WORKSHOP_SYSTEM_PROMPT = """You are Aura's Drone Workshop assistant. You help users build a saved Drone — a reusable worker or chore that runs autonomously.

You accept normal-user chore language, not just coding requests. A user might say "remind me when a new PR is opened" or "tell me if a build fails" — interpret that naturally.

You are an interviewer, not a form-filler. Have a natural conversation to understand what the user wants. Ask focused follow-up questions only when you need more information to write a useful build brief.

When you have enough information, produce a build brief (type: "brief") with ready_to_build set to true.

The brief should capture:
- What the user wants the Drone to do
- When and how it should run (trigger, schedule, or on-demand)
- What access, credentials, or setup it needs
- Safety boundary — what it should NOT do
- What a good first-run test would prove

Do NOT try to engineer the implementation. Just gather the job, constraints, access needs, safety boundary, and success test.

External access or setup needs should be noted in the build_brief as context, not as a blocker. The Workshop should not decide what is buildable — it should just describe what's needed honestly.

Never tell the user that something cannot be built or is unavailable — just describe what's needed honestly in the build_brief.

Return ONLY valid JSON in one of these exact shapes (no extra prose):

Question when more info is needed:
{"type": "question", "message": "One focused question for the user."}

Brief when enough info is available:
{"type": "brief", "message": "Short summary for the user.", "ready_to_build": true, "build_brief": "Plain-language build brief describing the Drone, its trigger, access needs, safety boundary, and first-run test."}"""


# ---------------------------------------------------------------------------
# Runner (QObject)
# ---------------------------------------------------------------------------


class DroneWorkshopRunner(QObject):
    """Streams a workshop conversation through ``APIAgentBackend`` and yields
    a parsed ``DroneWorkshopResponse``.

    Signals
    -------
    contentDelta(str)
        Streaming text chunks from the LLM.
    responseReady(object)
        Emitted with a ``DroneWorkshopResponse`` when the stream completes.
    apiError(int, str)
        Emitted on API failure — status code (or 0) and error message.
    finished
        Always emitted at the end of a run, regardless of outcome.
    """

    contentDelta = Signal(str)
    responseReady = Signal(object)
    apiError = Signal(int, str)  # status_code, message
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cancel_event = threading.Event()
        self._backend: APIAgentBackend | None = None

    def cancel(self) -> None:
        """Request cancellation (thread-safe)."""
        self._cancel_event.set()

    def run(
        self,
        conversation: list[dict[str, str]],
        provider_id: str,
        model: str,
        thinking: ThinkingMode = "disabled",
        temperature: float = 0.4,
    ) -> None:
        """Execute a workshop turn.

        Parameters
        ----------
        conversation
            List of ``{"role": "user"/"assistant", "content": "..."}`` messages
            excluding the system prompt (which is prepended automatically).
        provider_id
            Provider key (e.g. ``"deepseek"``).
        model
            Model identifier string.
        thinking
            Thinking mode (``"off"``, ``"high"``, ``"max"``).
        temperature
            Sampling temperature.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": DRONE_WORKSHOP_SYSTEM_PROMPT},
            *conversation,
        ]

        backend = APIAgentBackend(provider=provider_id)
        self._backend = backend
        self._cancel_event.clear()

        full_text: list[str] = []

        try:
            stream = backend.stream(
                messages=messages,
                tools=None,
                model=model,
                thinking=thinking,
                cancel_event=self._cancel_event,
                temperature=temperature,
            )

            for event in stream:
                if self._cancel_event.is_set():
                    break

                if isinstance(event, ContentDelta):
                    full_text.append(event.text)
                    self.contentDelta.emit(event.text)
                elif isinstance(event, ApiError):
                    self.apiError.emit(event.status_code or 0, event.message)
                    return
                elif isinstance(event, Done):
                    pass  # stream ended normally

            # Parse the accumulated response
            response = parse_workshop_response("".join(full_text))
            self.responseReady.emit(response)

        except Exception as exc:
            self.apiError.emit(0, str(exc))

        finally:
            self._backend = None
            self.finished.emit()
