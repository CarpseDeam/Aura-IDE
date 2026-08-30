"""Resolve one immutable Agent definition to an executable hosted model."""
from __future__ import annotations

from dataclasses import dataclass

from aura.agents.delegation import DelegationFailure
from aura.agents.models import AgentThinking, ModelTarget


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    provider: str
    model: str
    thinking: str


def resolve_model_target(
    target: ModelTarget,
    thinking: AgentThinking,
    *,
    inherited_provider: str,
    inherited_model: str,
    inherited_thinking: str,
) -> tuple[ResolvedTarget | None, DelegationFailure | None, str]:
    """Resolve exactly one inherited or explicit provider/model pair."""
    from aura.config import has_usable_provider_configuration
    from aura.providers.registry import provider_registry

    if target.inherits:
        provider = str(inherited_provider or "").strip()
        model = str(inherited_model or "").strip()
        if not provider or not model:
            return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
                "This agent inherits Aura's provider and model, but the current "
                "turn has no resolved provider/model to inherit."
            )
    elif not target.is_complete:
        return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
            "This agent names only half a model target. A definition must either "
            "inherit both the provider and the model, or name both."
        )
    else:
        provider = target.provider.strip()
        model = target.model.strip()

    if not provider_registry.has(provider):
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            f"This build does not know a provider called '{provider}'."
        )
    kind = provider_registry.get(provider).kind
    if kind != "api_key":
        return None, DelegationFailure.PROVIDER_UNSUPPORTED, (
            f"Provider '{provider}' is a '{kind}' provider. Agents currently run "
            "only on hosted API providers."
        )
    if not has_usable_provider_configuration(provider):
        return None, DelegationFailure.PROVIDER_NOT_CONFIGURED, (
            f"No API key is configured for '{provider}'. Add one in "
            "Settings → API Keys, or point this agent at a provider that has one."
        )

    resolved_thinking = (
        str(inherited_thinking or "off") if thinking.inherits else thinking.value
    )
    return ResolvedTarget(provider, model, resolved_thinking), None, ""


__all__ = ["ResolvedTarget", "resolve_model_target"]
