"""Per-model context budgeting.

The send path used to prune history against a single hardcoded 60K-token cap
regardless of which model was answering. That cap was simultaneously too small
for large-window models (evidence was crushed for no reason) and too generous
for small-window ones (no room was left for the response, so the last-resort
truncation pass fired on the *current* turn's source reads).

This module resolves the active model to an explicit budget:

    reserve      = max(model max_output_tokens, MIN_OUTPUT_RESERVE_TOKENS)
    working_set  = (context_window - reserve) * CONTEXT_WORKING_SET_FRACTION

The working set is deliberately a fraction of what is left after the reserve —
we never try to fill the advertised window. Models with no catalog metadata
(dynamically discovered, renamed, or unknown) fall back to a small window
rather than optimistically assuming a large one.
"""

from __future__ import annotations

from dataclasses import dataclass

from aura.config import (
    CONTEXT_WORKING_SET_FRACTION,
    FALLBACK_CONTEXT_WINDOW_TOKENS,
    FALLBACK_MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_RESERVE_TOKENS,
    MIN_WORKING_SET_TOKENS,
)
from aura.providers.base import ModelInfo
from aura.providers.registry import provider_registry


@dataclass(frozen=True)
class ModelBudget:
    """Resolved token budget for one model round."""

    model_id: str
    context_window_tokens: int
    output_reserve_tokens: int
    working_set_tokens: int
    is_fallback: bool

    def describe(self) -> str:
        source = "fallback" if self.is_fallback else "catalog"
        return (
            f"model={self.model_id} window={self.context_window_tokens} "
            f"reserve={self.output_reserve_tokens} "
            f"working_set={self.working_set_tokens} source={source}"
        )


def _lookup_model_info(model_id: str) -> ModelInfo | None:
    """Find catalog metadata for a model id across every registered provider."""
    if not model_id:
        return None
    for spec in provider_registry.all().values():
        info = spec.models.get(model_id)
        if info is not None:
            return info
    # Provider-qualified ids ("deepseek/deepseek-v4-flash") and bare ids refer
    # to the same underlying model; accept either spelling before giving up.
    bare = model_id.split("/")[-1]
    if bare != model_id:
        for spec in provider_registry.all().values():
            info = spec.models.get(bare)
            if info is not None:
                return info
    return None


def resolve_model_budget(model_id: str | None) -> ModelBudget:
    """Return the working-set budget to use for `model_id`.

    Never raises: an unknown model, a missing catalog entry, or nonsense
    metadata all resolve to the conservative fallback budget.
    """
    resolved_id = model_id or ""
    info = _lookup_model_info(resolved_id)

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

    working = int((window - reserve) * CONTEXT_WORKING_SET_FRACTION)
    working = max(MIN_WORKING_SET_TOKENS, working)
    # Even the floor must not exceed what the window can physically hold.
    working = min(working, max(1, window - reserve))

    return ModelBudget(
        model_id=resolved_id or "unknown",
        context_window_tokens=window,
        output_reserve_tokens=reserve,
        working_set_tokens=working,
        is_fallback=is_fallback,
    )
