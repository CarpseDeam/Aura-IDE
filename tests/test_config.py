"""Tests for aura.config — provider registry, settings, and model catalog."""

from __future__ import annotations

import pytest
from aura.config import (
    PROVIDERS,
    AppSettings,
    resolve_api_key,
)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

def test_all_five_providers_registered():
    """The PROVIDERS dict should contain all five providers."""
    assert set(PROVIDERS.keys()) == {"deepseek", "openai", "google", "openrouter", "anthropic"}


def test_provider_ids_are_valid():
    """Every provider's id should match its key."""
    for key, cfg in PROVIDERS.items():
        assert cfg.id == key


def test_provider_bases_are_urls():
    """Every base_url should start with https://."""
    for cfg in PROVIDERS.values():
        assert cfg.base_url.startswith("https://")


def test_provider_has_env_key():
    """Every provider should have a non-empty env_key."""
    for cfg in PROVIDERS.values():
        assert cfg.env_key
        assert "_API_KEY" in cfg.env_key


def test_provider_has_default_model():
    """Every provider should have a non-empty default_model string."""
    for cfg in PROVIDERS.values():
        assert cfg.default_model
        assert isinstance(cfg.default_model, str)


def test_anthropic_provider_config():
    """Verify specific Anthropic provider configuration."""
    anthropic = PROVIDERS["anthropic"]
    assert anthropic.label == "Anthropic"
    assert anthropic.base_url == "https://api.anthropic.com/v1"
    assert anthropic.env_key == "ANTHROPIC_API_KEY"
    assert anthropic.default_thinking == "high"
    assert anthropic.default_model == "claude-sonnet-4-20250514"
    # Models and pricing should be empty dicts (dynamically fetched)
    assert anthropic.models == {}
    assert anthropic.pricing == {}


def test_deepseek_provider_config():
    """Verify DeepSeek provider configuration."""
    ds = PROVIDERS["deepseek"]
    assert ds.label == "DeepSeek"
    assert ds.base_url == "https://api.deepseek.com"
    assert ds.env_key == "DEEPSEEK_API_KEY"
    assert ds.default_thinking == "high"


def test_openai_provider_config():
    """Verify OpenAI provider configuration."""
    oai = PROVIDERS["openai"]
    assert oai.label == "OpenAI"
    assert oai.base_url == "https://api.openai.com/v1"
    assert oai.env_key == "OPENAI_API_KEY"
    assert oai.default_thinking == "off"


# ---------------------------------------------------------------------------
# resolve_api_key
# ---------------------------------------------------------------------------

def test_resolve_api_key_from_env(monkeypatch):
    """resolve_api_key should read from the environment variable."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    key = resolve_api_key("anthropic")
    assert key == "sk-ant-123"


def test_resolve_api_key_missing(monkeypatch):
    """When env var is not set, should raise RuntimeError."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="No API key found"):
        resolve_api_key("anthropic")


def test_resolve_api_key_unknown_provider():
    """An unknown provider should raise KeyError (not in PROVIDERS dict)."""
    with pytest.raises(KeyError):
        resolve_api_key("nonexistent")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AppSettings
# ---------------------------------------------------------------------------

def test_app_settings_defaults():
    """AppSettings should have sensible defaults."""
    s = AppSettings()
    assert s.provider == "deepseek"
    assert s.default_planner_thinking == "high"
    assert s.default_worker_thinking == "high"
    assert s.default_planner_model == "deepseek-v4-flash"
    assert s.default_worker_model == "deepseek-v4-pro"


def test_app_settings_to_from_dict_roundtrip():
    """asdict() → from_dict() should be lossless for key fields."""
    from dataclasses import asdict

    original = AppSettings(
        provider="openai",
        default_planner_model="gpt-4o",
        default_planner_thinking="off",
        default_worker_model="gpt-4o-mini",
        default_worker_thinking="off",
        temperature=0.5,
        worker_temperature=0.1,
    )
    data = asdict(original)
    restored = AppSettings.from_dict(data)
    assert restored.provider == original.provider
    assert restored.default_planner_model == original.default_planner_model
    assert restored.default_planner_thinking == original.default_planner_thinking
    assert restored.default_worker_model == original.default_worker_model
    assert restored.default_worker_thinking == original.default_worker_thinking
    assert restored.temperature == original.temperature
    assert restored.worker_temperature == original.worker_temperature


def test_app_settings_from_dict_partial():
    """from_dict should fill in defaults for missing keys."""
    partial = {"provider": "google", "default_planner_model": "gemini-2.0-flash"}
    s = AppSettings.from_dict(partial)
    assert s.provider == "google"
    assert s.default_planner_model == "gemini-2.0-flash"
    # Defaults for unspecified fields
    assert s.default_planner_thinking == "high"
    assert s.default_worker_model == "deepseek-v4-pro"
