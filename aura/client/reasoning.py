"""Map the user's reasoning selection onto one provider request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.providers.base import ThinkingMode, normalize_thinking_mode

#: How the ``reasoning_effort`` parameter was decided for one request. Logged
#: verbatim so a request can be diagnosed from a normal log file.
EFFORT_EXPLICIT = "explicit_user_selection"
EFFORT_OMITTED_DISABLED = "omitted_reasoning_disabled"


@dataclass(frozen=True)
class ReasoningRequest:
    """The reasoning-related parts of one provider request.

    ``reasoning_effort is None`` means the parameter is deliberately *not*
    sent — the caller must omit the key entirely rather than send a null.
    """

    thinking: ThinkingMode
    provider: str
    extra_body: dict[str, Any] | None
    reasoning_effort: str | None
    send_temperature: bool
    effort_policy: str

    @property
    def effort_sent(self) -> bool:
        return self.reasoning_effort is not None

    def describe(self) -> str:
        return (
            f"thinking={self.thinking} provider={self.provider} "
            f"reasoning_effort={self.reasoning_effort or '<omitted>'} "
            f"effort_sent={self.effort_sent} effort_policy={self.effort_policy}"
        )


def _explicit_effort(thinking: ThinkingMode) -> str:
    """Return the effort string for an explicit high/max selection."""
    return "high" if thinking == "high" else "max"


def resolve_reasoning_request(
    provider: str, thinking: ThinkingMode
) -> ReasoningRequest:
    """Return the reasoning request for *provider* under the user's *thinking*.

    Never raises or escalates: a legacy or unrecognised value uses the High
    default rather than silently promoting to Max.
    """
    mode = normalize_thinking_mode(thinking) or "high"

    if provider == "deepseek":
        if mode == "off":
            return ReasoningRequest(
                thinking=mode,
                provider=provider,
                extra_body={"thinking": {"type": "disabled"}},
                reasoning_effort=None,
                send_temperature=True,
                effort_policy=EFFORT_OMITTED_DISABLED,
            )
        # Per DeepSeek docs, temperature/top_p/penalties are ignored while
        # thinking is enabled, so they are not sent.
        return ReasoningRequest(
            thinking=mode,
            provider=provider,
            extra_body={"thinking": {"type": "enabled"}},
            reasoning_effort=_explicit_effort(mode),
            send_temperature=False,
            effort_policy=EFFORT_EXPLICIT,
        )

    # OpenAI-compatible providers (openai, openrouter, and anything else routed
    # through the chat-completions path).
    if mode == "off":
        return ReasoningRequest(
            thinking=mode,
            provider=provider,
            extra_body=None,
            reasoning_effort=None,
            send_temperature=True,
            effort_policy=EFFORT_OMITTED_DISABLED,
        )
    return ReasoningRequest(
        thinking=mode,
        provider=provider,
        extra_body=None,
        reasoning_effort=_explicit_effort(mode),
        send_temperature=False,
        effort_policy=EFFORT_EXPLICIT,
    )


__all__ = [
    "EFFORT_EXPLICIT",
    "EFFORT_OMITTED_DISABLED",
    "ReasoningRequest",
    "resolve_reasoning_request",
]
