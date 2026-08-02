"""Map the user's reasoning selection onto one provider request.

The user picks one of ``off · auto · high · max`` and that selection is sent,
never reinterpreted. This module is the whole translation layer:

* **off** — reasoning disabled, temperature sent, no effort parameter.
* **high** / **max** — the effort is stated *explicitly* to the provider.
* **auto** — the provider decides. Aura either selects the provider's own
  documented automatic/adaptive mode, or omits the effort parameter so the
  provider applies its documented default.

There is deliberately no complexity scoring, no failure counting, and no
escalation ladder here. Aura does not guess how hard a task is; ``auto``
means the *provider's* native selection, which is the only party that can see
the prompt at inference time. If a provider has no native automatic mode, its
existing deterministic default behaviour is used and the log line says so —
Aura never invents a difficulty estimate to fill the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.providers.base import ThinkingMode

#: How the ``reasoning_effort`` parameter was decided for one request. Logged
#: verbatim so a request can be diagnosed from a normal log file.
EFFORT_EXPLICIT = "explicit_user_selection"
EFFORT_OMITTED_DISABLED = "omitted_reasoning_disabled"
EFFORT_OMITTED_PROVIDER_AUTO = "omitted_provider_native_auto"
EFFORT_OMITTED_PROVIDER_DEFAULT = "omitted_provider_default"


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

    Never raises and never escalates: an unrecognised mode is treated as
    ``auto`` (let the provider decide) rather than silently promoted to max.
    """
    mode = thinking if thinking in {"off", "auto", "high", "max"} else "auto"

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
        if mode == "auto":
            # DeepSeek selects its own effort when thinking is enabled and no
            # reasoning_effort is supplied. Sending one would override exactly
            # the native selection the user asked for.
            return ReasoningRequest(
                thinking=mode,
                provider=provider,
                extra_body={"thinking": {"type": "enabled"}},
                reasoning_effort=None,
                send_temperature=False,
                effort_policy=EFFORT_OMITTED_PROVIDER_AUTO,
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
    # through the chat-completions path). None of them expose an "auto" effort
    # value, but all of them apply a documented default when the parameter is
    # absent — omission *is* the native automatic behaviour here.
    if mode == "off":
        return ReasoningRequest(
            thinking=mode,
            provider=provider,
            extra_body=None,
            reasoning_effort=None,
            send_temperature=True,
            effort_policy=EFFORT_OMITTED_DISABLED,
        )
    if mode == "auto":
        return ReasoningRequest(
            thinking=mode,
            provider=provider,
            extra_body=None,
            reasoning_effort=None,
            send_temperature=False,
            effort_policy=EFFORT_OMITTED_PROVIDER_DEFAULT,
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
    "EFFORT_OMITTED_PROVIDER_AUTO",
    "EFFORT_OMITTED_PROVIDER_DEFAULT",
    "ReasoningRequest",
    "resolve_reasoning_request",
]
