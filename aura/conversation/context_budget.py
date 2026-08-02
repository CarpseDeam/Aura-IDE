"""Per-model context budgeting.

The send path used to prune history against a single hardcoded 60K-token cap
regardless of which model was answering. That cap was simultaneously too small
for large-window models (evidence was crushed for no reason) and too generous
for small-window ones (no room was left for the response, so the last-resort
truncation pass fired on the *current* turn's source reads).

This module resolves the active model to an explicit budget in two clearly
separated steps:

1. **Model capacity** — what the provider says the model can physically hold::

       reserve  = max(model max_output_tokens, MIN_OUTPUT_RESERVE_TOKENS)
       derived  = (context_window - reserve) * CONTEXT_WORKING_SET_FRACTION

   The derived working set is deliberately a fraction of what is left after
   the reserve — we never try to fill the advertised window. Models with no
   catalog metadata (dynamically discovered, renamed, or unknown) fall back to
   a small window rather than optimistically assuming a large one.

2. **Product spending policy** — what Aura is willing to *spend* on an
   ordinary request, from ``PROVIDER_WORKING_SET_CAP_TOKENS``. This is not
   model metadata; a provider absent from that map is uncapped.

The effective working set is the minimum of the two. Keeping them separate is
the point: a 1M-token model stays reported as a 1M-token model even while
ordinary coding turns are capped, and nobody is tempted to encode a fake window
in the catalog to control cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from aura.config import (
    CONTEXT_WORKING_SET_FRACTION,
    FALLBACK_CONTEXT_WINDOW_TOKENS,
    FALLBACK_MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_RESERVE_TOKENS,
    MIN_WORKING_SET_TOKENS,
    PROVIDER_WORKING_SET_CAP_TOKENS,
)
from aura.providers.base import ModelInfo
from aura.providers.registry import provider_registry


@dataclass(frozen=True)
class ModelBudget:
    """Resolved token budget for one model round.

    ``derived_working_set_tokens`` is pure model capacity; ``policy_cap_tokens``
    is pure product policy (``None`` when the provider is uncapped); and
    ``working_set_tokens`` is the effective minimum actually applied. They are
    kept as three separate fields so a log line or a test can tell which one
    constrained the turn.
    """

    model_id: str
    provider_id: str
    context_window_tokens: int
    output_reserve_tokens: int
    derived_working_set_tokens: int
    policy_cap_tokens: int | None
    working_set_tokens: int
    is_fallback: bool

    @property
    def capped_by_policy(self) -> bool:
        """Whether the product cap — not model capacity — set the budget."""
        return (
            self.policy_cap_tokens is not None
            and self.policy_cap_tokens < self.derived_working_set_tokens
        )

    def describe(self) -> str:
        source = "fallback" if self.is_fallback else "catalog"
        cap = "none" if self.policy_cap_tokens is None else str(self.policy_cap_tokens)
        return (
            f"model={self.model_id} provider={self.provider_id} "
            f"window={self.context_window_tokens} "
            f"reserve={self.output_reserve_tokens} "
            f"derived={self.derived_working_set_tokens} "
            f"policy_cap={cap} "
            f"working_set={self.working_set_tokens} "
            f"capped_by_policy={self.capped_by_policy} source={source}"
        )


def _lookup_model_info(model_id: str) -> tuple[str, ModelInfo] | None:
    """Find the provider id and catalog metadata for a model id."""
    if not model_id:
        return None
    for provider_id, spec in provider_registry.all().items():
        info = spec.models.get(model_id)
        if info is not None:
            return provider_id, info
    # Provider-qualified ids ("deepseek/deepseek-v4-flash") and bare ids refer
    # to the same underlying model; accept either spelling before giving up.
    bare = model_id.split("/")[-1]
    if bare != model_id:
        for provider_id, spec in provider_registry.all().items():
            info = spec.models.get(bare)
            if info is not None:
                return provider_id, info
    return None


def _policy_cap_for(model_id: str, provider_id: str) -> int | None:
    """Return the product working-set cap that applies, or ``None`` if uncapped.

    Matches on the resolved provider first. Provider-qualified model ids
    ("deepseek/deepseek-v4-flash" served through OpenRouter) are *not* capped by
    the underlying vendor's policy — the cap belongs to the provider Aura is
    actually billing against.
    """
    cap = PROVIDER_WORKING_SET_CAP_TOKENS.get(provider_id)
    if cap is None or cap <= 0:
        return None
    return cap


def resolve_model_budget(model_id: str | None) -> ModelBudget:
    """Return the working-set budget to use for `model_id`.

    Never raises: an unknown model, a missing catalog entry, or nonsense
    metadata all resolve to the conservative fallback budget.
    """
    resolved_id = model_id or ""
    found = _lookup_model_info(resolved_id)
    provider_id, info = found if found is not None else ("", None)

    window = info.context_window_tokens if info is not None else 0
    max_output = info.max_output_tokens if info is not None else 0

    is_fallback = window <= 0
    if is_fallback:
        window = FALLBACK_CONTEXT_WINDOW_TOKENS
    if max_output <= 0:
        max_output = FALLBACK_MAX_OUTPUT_TOKENS

    reserve = max(MIN_OUTPUT_RESERVE_TOKENS, max_output)
    # A model that claims it can emit its whole window as output would leave no
    # input room at all; cap the reserve at half the window.
    reserve = min(reserve, max(1, window // 2))

    derived = int((window - reserve) * CONTEXT_WORKING_SET_FRACTION)
    derived = max(MIN_WORKING_SET_TOKENS, derived)
    # Even the floor must not exceed what the window can physically hold.
    derived = min(derived, max(1, window - reserve))

    policy_cap = _policy_cap_for(resolved_id, provider_id)
    working = derived if policy_cap is None else min(derived, policy_cap)
    # A cap below the floor would make coding impossible; the floor wins.
    working = max(working, min(derived, MIN_WORKING_SET_TOKENS))

    return ModelBudget(
        model_id=resolved_id or "unknown",
        provider_id=provider_id or "unknown",
        context_window_tokens=window,
        output_reserve_tokens=reserve,
        derived_working_set_tokens=derived,
        policy_cap_tokens=policy_cap,
        working_set_tokens=working,
        is_fallback=is_fallback,
    )
