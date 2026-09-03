from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from aura.config import (
    fetch_provider_models,
    has_usable_provider_configuration,
    load_dynamic_catalog,
    save_dynamic_catalog,
)
from aura.providers.base import ModelInfo
from aura.providers.local_openai import (
    DEFAULT_LOCAL_OPENAI_BASE_URL,
    is_valid_local_openai_base_url,
    normalize_local_openai_base_url,
    require_valid_local_openai_base_url,
)
from aura.providers.registry import ProviderRegistry, provider_registry
from aura.settings import AppSettings, load_settings, save_settings


def _model(mid: str, *, price: float = 0.0) -> ModelInfo:
    return ModelInfo(
        id=mid,
        label=mid,
        input_per_m_usd=price,
        output_per_m_usd=price,
        cache_hit_per_m_usd=price,
    )


@pytest.fixture
def local_spec():
    spec = provider_registry.get("local_openai")
    base_url = spec.base_url
    models = dict(spec.models)
    pricing = dict(spec.pricing)
    spec.models.clear()
    spec.pricing.clear()
    provider_registry.configure_local_base_url(DEFAULT_LOCAL_OPENAI_BASE_URL)
    try:
        yield spec
    finally:
        spec.models.clear()
        spec.models.update(models)
        spec.pricing.clear()
        spec.pricing.update(pricing)
        provider_registry.configure_local_base_url(base_url)


def test_local_provider_has_no_invented_model(local_spec) -> None:
    assert local_spec.kind == "local"
    assert local_spec.base_url == DEFAULT_LOCAL_OPENAI_BASE_URL
    assert local_spec.default_model == ""
    assert local_spec.models == {}


@pytest.mark.parametrize(
    ("raw", "normalized", "valid"),
    [
        (" http://127.0.0.1:11434/v1/ ", "http://127.0.0.1:11434/v1", True),
        ("https://models.lan/openai/v1", "https://models.lan/openai/v1", True),
        ("127.0.0.1:11434/v1", "127.0.0.1:11434/v1", False),
        ("file:///tmp/model", "file:///tmp/model", False),
        ("http://localhost:bad/v1", "http://localhost:bad/v1", False),
        ("", "", False),
    ],
)
def test_local_endpoint_normalization_and_validation(
    raw: str, normalized: str, valid: bool
) -> None:
    assert normalize_local_openai_base_url(raw) == normalized
    assert is_valid_local_openai_base_url(raw) is valid
    if valid:
        assert require_valid_local_openai_base_url(raw) == normalized
    else:
        with pytest.raises(ValueError, match="absolute http"):
            require_valid_local_openai_base_url(raw)


def test_local_endpoint_persists_and_configures_runtime(
    tmp_path, monkeypatch, local_spec
) -> None:
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("aura.settings.settings_path", lambda: config_file)

    settings = AppSettings(local_openai_base_url=" http://localhost:1234/v1/ ")
    save_settings(settings)

    assert settings.local_openai_base_url == "http://localhost:1234/v1"
    assert json.loads(config_file.read_text(encoding="utf-8"))[
        "local_openai_base_url"
    ] == "http://localhost:1234/v1"
    assert local_spec.base_url == "http://localhost:1234/v1"
    assert load_settings().local_openai_base_url == "http://localhost:1234/v1"


def test_local_usability_requires_endpoint_and_real_model(local_spec) -> None:
    assert has_usable_provider_configuration("local_openai") is False

    local_spec.models["qwen-local"] = _model("qwen-local")
    assert has_usable_provider_configuration("local_openai") is True

    provider_registry.configure_local_base_url("not-a-url")
    assert has_usable_provider_configuration("local_openai") is False


def test_saved_local_selection_survives_before_catalog_hydration(local_spec) -> None:
    settings = AppSettings.from_dict(
        {
            "provider": "local_openai",
            "default_model": "qwen-local",
            "local_openai_base_url": DEFAULT_LOCAL_OPENAI_BASE_URL,
        }
    )

    assert settings.provider == "local_openai"
    assert settings.default_model == "qwen-local"
    assert has_usable_provider_configuration("local_openai") is False


def test_local_client_uses_override_and_dummy_sdk_key(monkeypatch, local_spec) -> None:
    import aura.client.deepseek as client_module

    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(client_module, "OpenAI", FakeOpenAI)
    client = provider_registry.create_client(
        "local_openai", base_url="http://localhost:8080/v1/"
    )

    assert client.provider == "local_openai"
    assert captured["api_key"] == "local"
    assert str(captured["base_url"]) == "http://localhost:8080/v1"
    # Testing an unsaved value must not change production configuration.
    assert local_spec.base_url == DEFAULT_LOCAL_OPENAI_BASE_URL


def test_local_stream_keeps_tools_on_chat_completions_and_forces_thinking_off(
    monkeypatch, local_spec
) -> None:
    import aura.client.deepseek as client_module

    class FakeOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    captured: dict[str, Any] = {}

    def fake_stream_chat_completions(**kwargs: Any) -> Iterator[Any]:
        captured.update(kwargs)
        return iter(())

    monkeypatch.setattr(client_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        client_module, "stream_chat_completions", fake_stream_chat_completions
    )

    tools = [{"type": "function", "function": {"name": "read_file"}}]
    client = provider_registry.create_client("local_openai")
    assert list(client.stream([], tools, "qwen-local", "high")) == []
    assert captured["provider"] == "local_openai"
    assert captured["tools"] is tools
    assert captured["thinking"] == "off"
    assert captured["chat_protocol"] == "openai_chat"


def test_local_discovery_override_and_zero_prices_on_id_collision(
    monkeypatch, local_spec
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def fetch_raw_models(self) -> list[dict[str, str]]:
            return [{"id": "gpt-5.4"}, {"id": "qwen-local"}]

    def fake_create_client(provider_id: str, *, base_url: str | None = None):
        captured.update(provider_id=provider_id, base_url=base_url)
        return FakeClient()

    monkeypatch.setattr(provider_registry, "create_client", fake_create_client)
    monkeypatch.setattr("aura.config.refresh_provider_pricing", lambda _pid: None)

    models, pricing, error = fetch_provider_models(
        "local_openai", base_url="http://localhost:1234/v1"
    )

    assert error is None
    assert captured == {
        "provider_id": "local_openai",
        "base_url": "http://localhost:1234/v1",
    }
    assert set(models) == {"gpt-5.4", "qwen-local"}
    assert models["gpt-5.4"].input_per_m_usd == 0.0
    assert models["gpt-5.4"].context_window_tokens == 0
    assert pricing["gpt-5.4"] == {
        "in_miss": 0.0,
        "in_hit": 0.0,
        "out": 0.0,
    }


def test_local_cache_is_a_zero_priced_snapshot(
    tmp_path, monkeypatch, local_spec
) -> None:
    monkeypatch.setattr("aura.config.config_dir", lambda: tmp_path)
    local_spec.models["stale"] = _model("stale")
    local_spec.pricing["stale"] = {"in_miss": 1.0, "in_hit": 1.0, "out": 1.0}

    save_dynamic_catalog(
        "local_openai",
        {"gpt-5.4": _model("gpt-5.4", price=99.0)},
        {"gpt-5.4": {"in_miss": 99.0, "in_hit": 99.0, "out": 99.0}},
    )
    load_dynamic_catalog()

    assert set(local_spec.models) == {"gpt-5.4"}
    assert local_spec.models["gpt-5.4"].input_per_m_usd == 0.0
    assert local_spec.pricing["gpt-5.4"] == {
        "in_miss": 0.0,
        "in_hit": 0.0,
        "out": 0.0,
    }


def test_external_cli_kind_still_cannot_create_api_client() -> None:
    registry = ProviderRegistry(
        {
            "legacy_cli": {
                "label": "Legacy CLI",
                "base_url": "",
                "env_key": "",
                "default_model": "legacy",
                "default_thinking": "off",
                "models": {},
                "pricing": {},
                "kind": "external_cli",
            }
        }
    )

    with pytest.raises(ValueError, match="has no API client"):
        registry.create_client("legacy_cli")
