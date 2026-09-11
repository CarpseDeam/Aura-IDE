"""Map the user's reasoning selection onto one provider request."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aura.providers.base import ModelInfo, ThinkingMode, normalize_thinking_mode

#: How the ``reasoning_effort`` parameter was decided for one request. Logged
#: verbatim so a request can be diagnosed from a normal log file.
EFFORT_EXPLICIT = "explicit_user_selection"
EFFORT_OMITTED_DISABLED = "omitted_reasoning_disabled"
#: The selected OpenAI model is documented as a non-reasoning model, so the
#: whole ``reasoning`` field is omitted no matter what the global UI selection
#: says. A thinking selection is a user preference, not a model capability.
EFFORT_OMITTED_UNSUPPORTED_MODEL = "omitted_model_is_not_a_reasoning_model"

# Model families that specialize in something other than text reasoning. They
# never take a reasoning field even when their base name would otherwise match.
_SPECIALIZED_MODEL_TOKENS: tuple[str, ...] = (
    "audio",
    "image",
    "realtime",
    "transcribe",
    "tts",
)

# A trailing dated snapshot suffix, e.g. ``gpt-5.5-2026-01-30``.
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# OpenAI reasoning models: the GPT-5 family and the o-series. ``-chat-latest``
# and the GPT-4.x families are documented non-reasoning models and do not
# match. Size aliases and dated snapshots of a reasoning model do match.
_OPENAI_REASONING_MODEL = re.compile(
    r"^(?:gpt-5(?:\.\d+)?(?:-(?:mini|nano|pro|codex))?|o[1-9](?:-(?:mini|pro))?)$"
)


def _base_model_id(model: str) -> str:
    """Return the lowercase model id with a dated snapshot suffix removed."""
    return _DATED_SNAPSHOT.sub("", str(model or "").strip().lower())


def openai_model_supports_reasoning(model: str) -> bool:
    """Return whether *model* is a documented OpenAI reasoning model."""
    base = _base_model_id(model)
    if any(token in base for token in _SPECIALIZED_MODEL_TOKENS):
        return False
    return bool(_OPENAI_REASONING_MODEL.match(base))


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
    def wire_effort(self) -> str | None:
        return self.reasoning_effort or (self.extra_body or {}).get("reasoning", {}).get("effort")

    @property
    def effort_sent(self) -> bool:
        return self.wire_effort is not None

    def describe(self) -> str:
        return (
            f"thinking={self.thinking} provider={self.provider} "
            f"reasoning_effort={self.wire_effort or '<omitted>'} "
            f"effort_sent={self.effort_sent} effort_policy={self.effort_policy}"
        )


def _explicit_effort(thinking: ThinkingMode) -> str:
    """Return the effort string for an explicit high/max selection."""
    return "high" if thinking == "high" else "max"


@dataclass(frozen=True)
class OpenRouterThinkingOption:
    mode: str
    label: str
    effort: str | None = None


_OPENROUTER_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal")


def openrouter_thinking_options(info: ModelInfo | None) -> tuple[OpenRouterThinkingOption, ...]:
    """Project advertised capabilities onto Aura's stored Off / High / Max.

    https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    Missing effort metadata offers an on/off switch, not invented effort levels.
    """
    metadata = (info.reasoning or {}) if info else {}
    parameters = info.supported_parameters if info else None
    if not metadata and parameters is not None and not any(
        p in parameters for p in ("reasoning", "reasoning_effort", "include_reasoning")
    ):
        return (OpenRouterThinkingOption("off", "Off (unsupported)"),)
    options = []
    if metadata.get("mandatory") is not True:
        options.append(OpenRouterThinkingOption("off", "Off"))
    efforts = metadata.get("supported_efforts", [])
    if efforts is None:
        efforts = _OPENROUTER_EFFORTS
    supported = [e for e in _OPENROUTER_EFFORTS if e in efforts] if isinstance(efforts, (list, tuple)) else []
    if supported:
        high = next((e for e in _OPENROUTER_EFFORTS[2:] if e in supported), supported[-1])
        options.append(OpenRouterThinkingOption("high", high.title(), high))
        if supported[0] != high:
            options.append(OpenRouterThinkingOption("max", f"Max ({supported[0]})", supported[0]))
    elif efforts == ["none"]:
        return (OpenRouterThinkingOption("off", "Off (unsupported)"),)
    else:
        label = "On" if metadata else "On (provider default; capabilities unknown)"
        options.append(OpenRouterThinkingOption("high", label))
    if metadata.get("mandatory") is True:
        options = [OpenRouterThinkingOption(o.mode, f"{o.label} (required)", o.effort) for o in options]
    return tuple(options)


def _resolve_openrouter_reasoning(mode: str, info: ModelInfo | None) -> ReasoningRequest:
    options = openrouter_thinking_options(info)
    option = next((o for o in options if o.mode == mode), options[-1] if mode == "max" else options[0])
    unsupported = len(options) == 1 and option.mode == "off"
    config: dict[str, Any] = {"enabled": option.mode != "off"}
    if option.effort is not None:
        config["effort"] = option.effort
    return ReasoningRequest(
        thinking=option.mode,
        provider="openrouter",
        extra_body=None if unsupported else {"reasoning": config},
        reasoning_effort=None,
        send_temperature=option.mode == "off",
        effort_policy=(
            EFFORT_OMITTED_UNSUPPORTED_MODEL if unsupported
            else EFFORT_OMITTED_DISABLED if option.mode == "off" else EFFORT_EXPLICIT
        ),
    )


def resolve_reasoning_request(
    provider: str, thinking: ThinkingMode, *, model_info: ModelInfo | None = None
) -> ReasoningRequest:
    """Return the reasoning request for *provider* under the user's *thinking*.

    Never raises or escalates: a legacy or unrecognised value uses the High
    default rather than silently promoting to Max.
    """
    mode = normalize_thinking_mode(thinking) or "high"

    if provider == "openrouter":
        return _resolve_openrouter_reasoning(mode, model_info)

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

    # Other OpenAI-compatible providers keep their existing request shape.
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


@dataclass(frozen=True)
class ResponsesReasoningRequest:
    """The reasoning-related parts of one Responses API request.

    ``reasoning is None`` means the whole ``reasoning`` field is deliberately
    omitted — the caller must not send an empty object or a null.
    """

    thinking: ThinkingMode
    provider: str
    model: str
    reasoning: dict[str, Any] | None
    send_temperature: bool
    effort_policy: str

    def describe(self) -> str:
        effort = (self.reasoning or {}).get("effort", "<omitted>")
        return (
            f"thinking={self.thinking} provider={self.provider} "
            f"model={self.model} reasoning_effort={effort} "
            f"effort_policy={self.effort_policy}"
        )


def resolve_responses_reasoning(
    *, provider: str, model: str, thinking: ThinkingMode
) -> ResponsesReasoningRequest:
    """Return the Responses reasoning shape for one selected provider/model.

    OpenAI is model-aware: a documented reasoning model keeps Aura's
    Off / High / Max mapping (``none`` / ``high`` / ``xhigh``) and does not
    send ``temperature``, which those models do not support.  A documented
    non-reasoning OpenAI model omits ``reasoning`` entirely and keeps
    ``temperature``; the global UI thinking selection never promotes an
    unsupported value onto the wire.

    Every other Responses provider — today DeepSeek — keeps its existing
    request shape unchanged.
    """
    mode = normalize_thinking_mode(thinking) or "high"
    # Aura's internal/UI/settings value for no reasoning is "off"; the wire
    # API rejects that literal and requires "none" instead.
    if provider == "openai":
        if not openai_model_supports_reasoning(model):
            return ResponsesReasoningRequest(
                thinking=mode,
                provider=provider,
                model=model,
                reasoning=None,
                send_temperature=True,
                effort_policy=EFFORT_OMITTED_UNSUPPORTED_MODEL,
            )
        effort = "none" if mode == "off" else "xhigh" if mode == "max" else mode
        return ResponsesReasoningRequest(
            thinking=mode,
            provider=provider,
            model=model,
            reasoning={"effort": effort},
            send_temperature=False,
            effort_policy=(
                EFFORT_OMITTED_DISABLED if mode == "off" else EFFORT_EXPLICIT
            ),
        )

    return ResponsesReasoningRequest(
        thinking=mode,
        provider=provider,
        model=model,
        reasoning={"effort": "none" if mode == "off" else mode},
        send_temperature=mode == "off",
        effort_policy=(
            EFFORT_OMITTED_DISABLED if mode == "off" else EFFORT_EXPLICIT
        ),
    )


__all__ = [
    "EFFORT_EXPLICIT",
    "EFFORT_OMITTED_DISABLED",
    "EFFORT_OMITTED_UNSUPPORTED_MODEL",
    "ReasoningRequest",
    "ResponsesReasoningRequest",
    "openai_model_supports_reasoning",
    "resolve_reasoning_request",
    "resolve_responses_reasoning",
]
