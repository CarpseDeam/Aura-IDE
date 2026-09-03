"""Resolve one immutable Agent definition to an executable model target."""
from __future__ import annotations

from dataclasses import dataclass

from aura.agents.delegation import DelegationFailure
from aura.agents.models import AgentThinking


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    provider: str
    model: str
    thinking: str


def resolve_agent_model(
    model: str,
    thinking: AgentThinking,
    *,
    provider: str,
    turn_model: str,
    turn_thinking: str,
    agent_provider: str = "",
) -> tuple[ResolvedTarget | None, DelegationFailure | None, str]:
    """Resolve all four inherited/explicit provider and model combinations."""
    from aura.config import (
        has_usable_provider_configuration,
        resolve_production_default_model,
    )
    from aura.providers.registry import provider_registry

    inherited_provider = str(provider or "").strip()
    requested_provider = str(agent_provider or "").strip()
    resolved_provider = requested_provider or inherited_provider
    if not resolved_provider:
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            "This turn has no resolved provider, so there is nothing for an "
            "agent to run on."
        )
    if not provider_registry.has(resolved_provider):
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            f"This build does not know a provider called '{resolved_provider}'."
        )
    spec = provider_registry.get(resolved_provider)
    if spec.kind not in {"api_key", "local"}:
        return None, DelegationFailure.PROVIDER_UNSUPPORTED, (
            f"Provider '{resolved_provider}' is a '{spec.kind}' provider and "
            "cannot back an Aura Agent."
        )

    resolved_model = str(model or "").strip()
    if not resolved_model:
        resolved_model = (
            str(resolve_production_default_model(resolved_provider) or "").strip()
            if requested_provider
            else str(turn_model or "").strip()
        )
    if not resolved_model:
        return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
            "This agent names no model of its own and its inherited or selected "
            "provider has no resolved model to use."
        )

    if not has_usable_provider_configuration(resolved_provider):
        settings_page = "Models" if spec.kind == "local" else "API Keys"
        return None, DelegationFailure.PROVIDER_NOT_CONFIGURED, (
            f"Provider '{resolved_provider}' is not configured on this machine. "
            f"Configure it in Settings → {settings_page}."
        )

    # Thinking is an independent Agent choice. ``inherit`` has always meant
    # the submitted Aura turn's selection, even when this definition qualifies
    # another hosted provider. Local OpenAI-compatible servers do not share a
    # portable reasoning parameter, so their frozen target is explicitly Off
    # rather than merely being projected to Off later by the transport.
    if spec.kind == "local":
        resolved_thinking = "off"
    elif thinking.inherits:
        resolved_thinking = str(turn_thinking or "off")
    else:
        resolved_thinking = thinking.value
    return ResolvedTarget(resolved_provider, resolved_model, resolved_thinking), None, ""


__all__ = ["ResolvedTarget", "resolve_agent_model"]
