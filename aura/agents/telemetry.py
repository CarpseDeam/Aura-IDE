"""Project aggregated private-child usage onto root conversation telemetry."""
from __future__ import annotations

from typing import Any

_LEGACY_TOKEN_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
    }
)


def delegation_usage_signals(
    extras: dict[str, Any] | None,
) -> tuple[tuple[str, str, int, int, int, int], ...]:
    """Return every exact provider/model usage group in supplied order."""
    source = extras or {}
    groups = source.get("delegation_usage_groups")
    rows: tuple[Any, ...]
    if isinstance(groups, list):
        rows = tuple(groups)
    else:
        data = source.get("delegation_usage")
        if not isinstance(data, dict) or not _LEGACY_TOKEN_FIELDS.intersection(data):
            return ()
        rows = (
            {
                "provider": source.get("delegation_provider"),
                "model": source.get("delegation_model"),
                **data,
            },
        )
    signals: list[tuple[str, str, int, int, int, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        if not provider or not model:
            continue
        try:
            signals.append(
                (
                    provider,
                    model,
                    max(0, int(row.get("prompt_tokens") or 0)),
                    max(0, int(row.get("completion_tokens") or 0)),
                    max(0, int(row.get("cache_hit_tokens") or 0)),
                    max(0, int(row.get("cache_miss_tokens") or 0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(signals)


def delegation_usage_signal(
    extras: dict[str, Any] | None,
) -> tuple[str, str, int, int, int, int] | None:
    """Backward-compatible singular projection for ordinary delegation."""
    signals = delegation_usage_signals(extras)
    return signals[0] if signals else None


__all__ = ["delegation_usage_signal", "delegation_usage_signals"]
