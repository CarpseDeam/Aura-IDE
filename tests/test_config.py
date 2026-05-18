"""Tests for aura.config — provider registry, settings, and model catalog."""

from __future__ import annotations

import json

import pytest
from aura.config import (
    AppSettings,
    fetch_provider_models,
    resolve_api_key,
)
from aura.models import PROVIDERS


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

def test_all_providers_registered():
    """The PROVIDERS dict should contain all four providers."""
    assert set(PROVIDERS.keys()) == {"deepseek", "openai", "openrouter", "anthropic"}


def test_provider_ids_are_valid():
    """Every provider's id should match its key."""
    for key, cfg in PROVIDERS.items():
        assert cfg.id == key


def test_provider_bases_are_urls():
    """Every base_url should start with https://."""
    for cfg in PROVIDERS.values():
        assert cfg.base_url.startswith("https://")


def test_provider_has_env_key():
    """Every provider should have a non-empty primary env_key."""
    for cfg in PROVIDERS.values():
        assert cfg.env_key
        assert cfg.env_key.startswith(("OPEN", "DEEPSEEK", "ANTHROPIC"))


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
    assert anthropic.default_model == "claude-sonnet-4-6"
    assert isinstance(anthropic.models, dict)


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
    assert s.default_planner_thinking == "off"
    assert s.default_worker_thinking == "high"
    assert s.default_planner_model == "deepseek-v4-flash"
    assert s.default_worker_model == "deepseek-v4-pro"
    # Verify DEEPSEEK_MODELS is pre-populated with both models
    from aura.providers.catalog import DEEPSEEK_MODELS
    assert "deepseek-v4-flash" in DEEPSEEK_MODELS
    assert "deepseek-v4-pro" in DEEPSEEK_MODELS


def test_app_settings_from_dict_empty_preserves_deepseek_worker_pro():
    """from_dict({}) should default to deepseek-v4-pro for worker model."""
    s = AppSettings.from_dict({})
    assert s.default_worker_model == "deepseek-v4-pro"
    assert s.default_planner_model == "deepseek-v4-flash"


def test_app_settings_from_dict_stale_model_falls_back_to_deepseek_pro():
    """A stale default_worker_model should fall back to deepseek-v4-pro."""
    data = {
        "provider": "deepseek",
        "planner_provider": "deepseek",
        "worker_provider": "deepseek",
        "default_worker_model": "some-non-existent-model",
    }
    s = AppSettings.from_dict(data)
    assert s.default_worker_model == "deepseek-v4-pro"


def test_app_settings_to_from_dict_roundtrip():
    """asdict() → from_dict() should be lossless for key fields."""
    from dataclasses import asdict

    original = AppSettings(
        provider="openai",
        planner_provider="openai",
        worker_provider="openai",
        default_model="gpt-4o",
        default_planner_model="gpt-4o",
        default_planner_thinking="off",
        default_worker_model="gpt-4o",
        default_worker_thinking="off",
        temperature=0.5,
        worker_temperature=0.1,
    )
    data = asdict(original)
    restored = AppSettings.from_dict(data)
    assert restored.provider == original.provider
    assert restored.planner_provider == original.planner_provider
    assert restored.worker_provider == original.worker_provider
    assert restored.default_model == original.default_model
    assert restored.default_planner_model == original.default_planner_model
    assert restored.default_planner_thinking == original.default_planner_thinking
    assert restored.default_worker_model == original.default_worker_model
    assert restored.default_worker_thinking == original.default_worker_thinking
    assert restored.temperature == original.temperature
    assert restored.worker_temperature == original.worker_temperature


def test_app_settings_from_dict_partial():
    """from_dict should fill in defaults for missing keys."""
    partial = {
        "provider": "openai",
        "planner_provider": "openai",
        "default_planner_model": "gpt-4o",
    }
    s = AppSettings.from_dict(partial)
    assert s.provider == "openai"
    # Fallback to provider default since list is empty
    assert s.default_model == "gpt-4o"
    assert s.default_planner_model == "gpt-4o"
    # Defaults for unspecified fields
    assert s.default_planner_thinking == "off"


def test_app_settings_from_dict_invalid_providers_fall_back(caplog):
    """Invalid provider IDs should not survive into runtime settings."""
    data = {
        "provider": "not-a-provider",
        "planner_provider": "bad-planner",
        "worker_provider": "bad-worker",
    }

    s = AppSettings.from_dict(data)

    assert s.provider == "deepseek"
    assert s.planner_provider == "deepseek"
    assert s.worker_provider == "deepseek"
    assert "Invalid provider value" in caplog.text


def test_app_settings_from_dict_invalid_models_fall_back(caplog):
    """Model defaults should be valid for their selected providers."""
    data = {
        "provider": "openai",
        "planner_provider": "anthropic",
        "worker_provider": "anthropic",
        "default_model": "some-random-model",
    }

    s = AppSettings.from_dict(data)

    # default_model is mirrored from planner_provider→default_planner_model
    assert s.default_model == "claude-sonnet-4-6"
    assert s.default_planner_model == "claude-sonnet-4-6"  # Fallback for Anthropic
    assert s.default_worker_model == "claude-sonnet-4-6"  # Fallback for Anthropic


# ---------------------------------------------------------------------------
# KeyManager integration
# ---------------------------------------------------------------------------


def test_stored_key_fallback(tmp_path, monkeypatch):
    """get_api_key should fall back to stored key when env var is not set."""
    from aura.key_manager import KeyManager
    import aura.key_manager

    # Reset singleton to ensure it picks up the monkeypatched config_dir
    aura.key_manager._key_manager = None

    # Create a temp keys.json
    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()

    # Monkeypatch config_dir to return our temp path
    monkeypatch.setattr("aura.config.config_dir", lambda: keys_dir)
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)

    # Ensure env var is not set
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    km = KeyManager()
    km.set_key("openai", "sk-test-key-123")

    # Now get_api_key should return the stored key
    from aura.config import get_api_key

    key = get_api_key("openai")
    assert key == "sk-test-key-123"


def test_stored_key_is_encrypted_on_disk(tmp_path, monkeypatch):
    """The keys.json file should contain Fernet tokens, not plaintext."""
    from aura.key_manager import KeyManager

    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)

    km = KeyManager()
    km.set_key("deepseek", "sk-plaintext-secret")

    data = json.loads(km._path.read_text("utf-8"))
    ciphertext = data["deepseek"]
    # Must be a Fernet token (starts with gAAAAA)
    assert ciphertext.startswith("gAAAAA")
    # Must NOT contain the plaintext
    assert "sk-plaintext-secret" not in ciphertext


def test_legacy_plaintext_migration(tmp_path, monkeypatch):
    """Legacy plaintext in keys.json should be auto-migrated to encrypted."""
    from aura.key_manager import KeyManager

    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)

    km = KeyManager()
    # Simulate old plaintext storage: write directly to file
    km._path.write_text(json.dumps({"deepseek": "sk-legacy-plaintext"}), encoding="utf-8")

    # get_key should return the plaintext
    key = km.get_key("deepseek")
    assert key == "sk-legacy-plaintext"

    # But now the file should contain a Fernet token
    data = json.loads(km._path.read_text("utf-8"))
    assert data["deepseek"].startswith("gAAAAA")


def test_key_manager_delete(tmp_path, monkeypatch):
    """Deleting a key should remove it from the file."""
    from aura.key_manager import KeyManager

    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)

    km = KeyManager()
    km.set_key("anthropic", "sk-ant-abc")
    assert km.has_key("anthropic")

    km.delete_key("anthropic")
    assert not km.has_key("anthropic")
    assert km.get_key("anthropic") is None


def test_get_key_missing_file(tmp_path, monkeypatch):
    """get_key should return None when keys.json doesn't exist."""
    from aura.key_manager import KeyManager

    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)

    km = KeyManager()
    assert km.get_key("openai") is None
    assert not km.has_key("openai")


def test_set_api_key_public_function(tmp_path, monkeypatch):
    """The public set_api_key in config.py should work."""
    from aura.config import set_api_key, get_api_key

    keys_dir = tmp_path / "Aura"
    keys_dir.mkdir()
    monkeypatch.setattr("aura.config.config_dir", lambda: keys_dir)
    monkeypatch.setattr("aura.key_manager.config_dir", lambda: keys_dir)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    set_api_key("deepseek", "sk-ds-public-test")
    key = get_api_key("deepseek")
    assert key == "sk-ds-public-test"


def test_redact_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """redact_secrets should replace all known API keys with [REDACTED]."""
    from aura.config import redact_secrets
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SECRET_DS_123")
    monkeypatch.setenv("TAVILY_API_KEY", "SECRET_TAVILY_789")

    text = "Error with key SECRET_DS_123. Search key: SECRET_TAVILY_789"
    redacted = redact_secrets(text)

    assert "SECRET_DS_123" not in redacted
    assert "SECRET_TAVILY_789" not in redacted
    assert redacted == "Error with key [REDACTED]. Search key: [REDACTED]"


# ---------------------------------------------------------------------------
# Provider architecture tests
# ---------------------------------------------------------------------------


def test_provider_registry_contains_exactly_four():
    """provider_registry should contain exactly the 4 supported providers."""
    from aura.providers.registry import provider_registry
    assert set(provider_registry.ids()) == {"deepseek", "openai", "openrouter", "anthropic"}


def test_provider_registry_does_not_contain_google():
    """provider_registry must not contain google_ai or vertex_ai."""
    from aura.providers.registry import provider_registry
    assert not provider_registry.has("google_ai")
    assert not provider_registry.has("vertex_ai")


def test_provider_registry_creates_client():
    """create_client should return a DeepSeekClient for each provider."""
    from aura.client.deepseek import DeepSeekClient
    from aura.providers.registry import provider_registry
    for pid in provider_registry.ids():
        client = provider_registry.create_client(pid)
        assert isinstance(client, DeepSeekClient)


def test_settings_migrates_google_ai_to_deepseek():
    """google_ai in settings should migrate to deepseek."""
    from aura.settings import AppSettings, DEFAULT_PROVIDER
    data = {"provider": "google_ai"}
    s = AppSettings.from_dict(data)
    assert s.provider == DEFAULT_PROVIDER
    assert s.provider == "deepseek"


def test_settings_migrates_vertex_ai_to_deepseek():
    """vertex_ai in settings should migrate to deepseek."""
    from aura.settings import AppSettings, DEFAULT_PROVIDER
    data = {"provider": "vertex_ai"}
    s = AppSettings.from_dict(data)
    assert s.provider == DEFAULT_PROVIDER
    assert s.provider == "deepseek"
