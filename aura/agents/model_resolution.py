"""Resolve one immutable Agent definition to an executable hosted model.

The provider is never the agent's to choose. Whatever provider Aura is set to
for the submitted turn is the provider the agent runs on, so a definition
that travels with a repository cannot point somebody else's machine at a
service they have no key for. The definition contributes a model — resolved
under that provider — or nothing at all, in which case the agent runs the
same model Aura is running.
"""
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
) -> tuple[ResolvedTarget | None, DelegationFailure | None, str]:
    """Resolve one agent's model under the turn's own provider."""
    from aura.config import has_usable_provider_configuration
    from aura.providers.registry import provider_registry

    resolved_provider = str(provider or "").strip()
    resolved_model = str(model or "").strip() or str(turn_model or "").strip()
    if not resolved_provider:
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            "This turn has no resolved provider, so there is nothing for an "
            "agent to run on."
        )
    if not resolved_model:
        return None, DelegationFailure.MODEL_TARGET_INCOMPLETE, (
            "This agent names no model of its own and the current turn has no "
            "resolved model to fall back to."
        )

    if not provider_registry.has(resolved_provider):
        return None, DelegationFailure.PROVIDER_UNKNOWN, (
            f"This build does not know a provider called '{resolved_provider}'."
        )
    kind = provider_registry.get(resolved_provider).kind
    if kind != "api_key":
        return None, DelegationFailure.PROVIDER_UNSUPPORTED, (
            f"Provider '{resolved_provider}' is a '{kind}' provider. Agents "
            "currently run only on hosted API providers."
        )
    if not has_usable_provider_configuration(resolved_provider):
        return None, DelegationFailure.PROVIDER_NOT_CONFIGURED, (
            f"No API key is configured for '{resolved_provider}'. Add one in "
            "Settings → API Keys."
        )

    resolved_thinking = (
        str(turn_thinking or "off") if thinking.inherits else thinking.value
    )
    return ResolvedTarget(resolved_provider, resolved_model, resolved_thinking), None, ""


__all__ = ["ResolvedTarget", "resolve_agent_model"]
