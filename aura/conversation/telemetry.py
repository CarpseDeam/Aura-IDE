"""Durable, conversation-scoped provider usage telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _token_count(value: Any) -> int:
    """Normalize provider and persisted token counts without estimating them."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def context_window_for_model(model_id: str) -> int:
    """Return the catalogued capacity for *model_id*, or zero when unknown."""
    from aura.models import PROVIDERS

    for provider in PROVIDERS.values():
        model = provider.models.get(model_id)
        if model is not None:
            return _token_count(model.context_window_tokens)
    return 0


@dataclass
class LatestContext:
    """The most recently provider-confirmed input-token snapshot."""

    model_id: str = ""
    input_tokens: int = 0
    context_window_tokens: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "context_window_tokens": self.context_window_tokens,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LatestContext":
        if not isinstance(data, dict):
            return cls()
        model_id = data.get("model_id")
        return cls(
            model_id=model_id if isinstance(model_id, str) else "",
            input_tokens=_token_count(data.get("input_tokens")),
            context_window_tokens=_token_count(data.get("context_window_tokens")),
        )


@dataclass
class ConversationTelemetry:
    """The sole normalized data shape for durable footer telemetry."""

    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    latest_context: LatestContext = field(default_factory=LatestContext)

    def record_usage(
        self,
        *,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
        context_window_tokens: int,
    ) -> None:
        """Accumulate usage and replace the provider-confirmed context snapshot."""
        prompt = _token_count(prompt)
        hit = _token_count(hit)
        miss = _token_count(miss)
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self.per_model.setdefault(model_id, {"hit": 0, "miss": 0, "out": 0})
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += _token_count(completion)
        self.latest_context = LatestContext(
            model_id=model_id,
            input_tokens=prompt,
            context_window_tokens=_token_count(context_window_tokens),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_model": {model: dict(counts) for model, counts in self.per_model.items()},
            "latest_context": self.latest_context.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConversationTelemetry":
        if not isinstance(data, dict):
            return cls()
        per_model_raw = data.get("per_model")
        per_model: dict[str, dict[str, int]] = {}
        if isinstance(per_model_raw, dict):
            for model_id, counts in per_model_raw.items():
                if isinstance(model_id, str) and isinstance(counts, dict):
                    per_model[model_id] = {
                        "hit": _token_count(counts.get("hit")),
                        "miss": _token_count(counts.get("miss")),
                        "out": _token_count(counts.get("out")),
                    }
        return cls(per_model=per_model, latest_context=LatestContext.from_dict(data.get("latest_context")))
