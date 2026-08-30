"""Project aggregated private-child usage onto root conversation telemetry."""
from __future__ import annotations

from typing import Any


def delegation_usage_signal(
    extras: dict[str, Any] | None,
) -> tuple[str, int, int, int, int] | None:
    """Return ``(model, prompt, completion, hit, miss)`` once per result."""
    data = (extras or {}).get("delegation_usage")
    model = str((extras or {}).get("delegation_model") or "")
    if not isinstance(data, dict) or not model:
        return None
    try:
        return (
            model,
            max(0, int(data.get("prompt_tokens") or 0)),
            max(0, int(data.get("completion_tokens") or 0)),
            max(0, int(data.get("cache_hit_tokens") or 0)),
            max(0, int(data.get("cache_miss_tokens") or 0)),
        )
    except (TypeError, ValueError):
        return None


__all__ = ["delegation_usage_signal"]
