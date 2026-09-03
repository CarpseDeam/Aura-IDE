"""Configuration helpers for Aura's OpenAI-compatible local provider."""

from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_LOCAL_OPENAI_BASE_URL = "http://127.0.0.1:11434/v1"


def normalize_local_openai_base_url(value: object) -> str:
    """Return the user's endpoint in the canonical persisted form.

    Validation is deliberately separate so malformed saved values remain
    visible and correctable in Settings instead of silently sending requests
    to a different endpoint.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def is_valid_local_openai_base_url(value: object) -> bool:
    """Return whether *value* is an absolute HTTP(S) API base URL."""
    normalized = normalize_local_openai_base_url(value)
    if not normalized:
        return False
    try:
        parsed = urlsplit(normalized)
        # Accessing ``port`` also rejects malformed/non-numeric ports.
        _ = parsed.port
    except (ValueError, TypeError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and not parsed.query
        and not parsed.fragment
    )


def require_valid_local_openai_base_url(value: object) -> str:
    """Return a normalized endpoint or raise a user-facing configuration error."""
    normalized = normalize_local_openai_base_url(value)
    if not is_valid_local_openai_base_url(normalized):
        raise ValueError(
            "Local Model endpoint must be an absolute http:// or https:// URL "
            "such as http://127.0.0.1:11434/v1."
        )
    return normalized
