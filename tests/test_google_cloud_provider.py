"""Tests for aura.providers.google_cloud — all offline, no live API calls."""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unload_google_cloud_modules() -> None:
    for mod_name in list(sys.modules):
        if "aura.providers.google_cloud" in mod_name:
            del sys.modules[mod_name]


def _fresh_provider_registry() -> object:
    """Re-import the registry with clean module state."""
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    return aura.providers.registry.provider_registry


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_parsing(monkeypatch):
    from aura.providers.google_cloud.config import (
        get_google_cloud_config,
        get_google_cloud_location,
        get_google_cloud_project,
    )

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-east1")

    assert get_google_cloud_project() == "my-project"
    assert get_google_cloud_location() == "us-east1"

    cfg = get_google_cloud_config()
    assert cfg["project"] == "my-project"
    assert cfg["location"] == "us-east1"


def test_config_defaults(monkeypatch):
    from aura.providers.google_cloud.config import (
        get_google_cloud_config,
        get_google_cloud_location,
        get_google_cloud_project,
    )

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    assert get_google_cloud_project() is None
    assert get_google_cloud_location() == "global"
    assert get_google_cloud_config()["project"] is None


def test_is_configured(monkeypatch):
    from aura.providers.google_cloud.config import is_configured

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert is_configured() is True

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    assert is_configured() is False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_detect_auth_mode_adc_from_env(tmp_path, monkeypatch):
    from aura.providers.google_cloud.auth import detect_auth_mode

    adc_file = tmp_path / "adc.json"
    adc_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_file))
    assert detect_auth_mode() == "adc"


def test_detect_auth_mode_unknown(monkeypatch, tmp_path):
    from aura.providers.google_cloud.auth import detect_auth_mode

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    # Monkeypatch the default ADC path to a non-existent directory
    monkeypatch.setattr(
        "aura.providers.google_cloud.auth._DEFAULT_ADC_PATH",
        Path(tmp_path) / "nonexistent" / "adc.json",
    )
    assert detect_auth_mode() == "unknown"


def test_check_adc_file_env(tmp_path, monkeypatch):
    from aura.providers.google_cloud.auth import check_adc_file

    adc_file = tmp_path / "my-creds.json"
    adc_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_file))
    assert check_adc_file() == str(adc_file)


def test_check_adc_file_none(monkeypatch, tmp_path):
    from aura.providers.google_cloud.auth import check_adc_file

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(
        "aura.providers.google_cloud.auth._DEFAULT_ADC_PATH",
        Path(tmp_path) / "nonexistent" / "adc.json",
    )
    assert check_adc_file() is None


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def test_signature_encoding_bytes():
    from aura.providers.google_cloud.signatures import encode_signature_safe

    result = encode_signature_safe(b"\x00\xff\xab")
    # Must not be str(bytes) — no "b'" prefix
    assert isinstance(result, str)
    assert not result.startswith("b'")
    assert not result.startswith('b"')
    assert len(result) > 0


def test_signature_encoding_str():
    from aura.providers.google_cloud.signatures import encode_signature_safe

    assert encode_signature_safe("hello world") == "hello world"


def test_signature_encoding_int():
    from aura.providers.google_cloud.signatures import encode_signature_safe

    result = encode_signature_safe(42)
    assert isinstance(result, str)


def test_no_raw_bytes_in_stored_messages():
    from aura.providers.google_cloud.signatures import make_message_json_safe

    msg = {
        "role": "assistant",
        "content": "text",
        "raw": b"binary data",
        "nested": {"deep": b"more binary"},
    }
    safe = make_message_json_safe(msg)
    assert not isinstance(safe["raw"], bytes)
    assert not isinstance(safe["nested"]["deep"], bytes)
    assert isinstance(safe["raw"], str)
    assert isinstance(safe["nested"]["deep"], str)


def test_signatures_not_merged():
    from aura.providers.google_cloud.signatures import make_message_json_safe

    sigs = {"sig_a": b"aaa", "sig_b": b"bbb"}
    safe = make_message_json_safe(sigs)
    assert safe["sig_a"] != safe["sig_b"]


def test_decode_signature_roundtrip():
    from aura.providers.google_cloud.signatures import (
        decode_signature,
        encode_signature_safe,
    )

    original = b"\x00\xff\xab\x12"
    encoded = encode_signature_safe(original)
    decoded = decode_signature(encoded)
    assert decoded == original


def test_decode_signature_plain_string():
    from aura.providers.google_cloud.signatures import decode_signature

    assert decode_signature("hello") == "hello"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_error_classification_400():
    from aura.providers.google_cloud.errors import BadRequestError, classify_error

    err = classify_error(400, "bad")
    assert isinstance(err, BadRequestError)
    assert err.status_code == 400


def test_error_classification_401():
    from aura.providers.google_cloud.errors import AuthError, classify_error

    err = classify_error(401, "unauthorized")
    assert isinstance(err, AuthError)


def test_error_classification_403():
    from aura.providers.google_cloud.errors import ForbiddenError, classify_error

    err = classify_error(403, "forbidden")
    assert isinstance(err, ForbiddenError)


def test_error_classification_404():
    from aura.providers.google_cloud.errors import NotFoundError, classify_error

    err = classify_error(404, "not found")
    assert isinstance(err, NotFoundError)


def test_error_classification_408():
    from aura.providers.google_cloud.errors import TransientError, classify_error

    err = classify_error(408, "timeout")
    assert isinstance(err, TransientError)


def test_error_classification_429():
    from aura.providers.google_cloud.errors import ResourceExhaustedError, classify_error

    err = classify_error(429, "exhausted")
    assert isinstance(err, ResourceExhaustedError)


def test_error_classification_500():
    from aura.providers.google_cloud.errors import TransientError, classify_error

    err = classify_error(500, "internal")
    assert isinstance(err, TransientError)


def test_error_classification_502():
    from aura.providers.google_cloud.errors import TransientError, classify_error

    err = classify_error(502, "bad gateway")
    assert isinstance(err, TransientError)


def test_error_classification_unknown():
    from aura.providers.google_cloud.errors import (
        AuthError,
        GoogleCloudError,
        TransientError,
        classify_error,
    )

    err = classify_error(418, "teapot")
    assert isinstance(err, GoogleCloudError)
    assert not isinstance(err, AuthError)
    assert not isinstance(err, TransientError)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_hit_and_cooling():
    from aura.providers.google_cloud.cooldown import CooldownManager

    cm = CooldownManager(cooldown_seconds=30.0)
    assert not cm.is_cooling()
    cm.hit()
    assert cm.is_cooling()


def test_cooldown_remaining():
    from aura.providers.google_cloud.cooldown import CooldownManager

    cm = CooldownManager(cooldown_seconds=5.0)
    cm.hit()
    remaining = cm.remaining()
    assert remaining > 0.0
    assert remaining <= 5.0


def test_cooldown_reset():
    from aura.providers.google_cloud.cooldown import CooldownManager

    cm = CooldownManager(cooldown_seconds=30.0)
    cm.hit()
    assert cm.is_cooling()
    cm.reset()
    assert not cm.is_cooling()
    assert cm.remaining() == 0.0


def test_cooldown_thread_safety():
    from aura.providers.google_cloud.cooldown import CooldownManager

    cm = CooldownManager(cooldown_seconds=0.01)
    errors: list[str] = []

    def _hammer() -> None:
        try:
            for _ in range(200):
                cm.hit()
                cm.is_cooling()
                cm.remaining()
                cm.reset()
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread-safety failures: {errors}"


# ---------------------------------------------------------------------------
# Tool mapping
# ---------------------------------------------------------------------------


def test_tool_mapping_empty():
    from aura.providers.google_cloud.mapping import aura_tools_to_google_declarations

    assert aura_tools_to_google_declarations([]) == []


def test_tool_mapping_single():
    from aura.providers.google_cloud.mapping import aura_tools_to_google_declarations

    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    decls = aura_tools_to_google_declarations(tools)
    assert len(decls) == 1
    assert decls[0]["name"] == "read_file"
    assert decls[0]["description"] == "Read a file"
    assert decls[0]["parameters"] == {"type": "object", "properties": {}}


def test_tool_mapping_multiple():
    from aura.providers.google_cloud.mapping import aura_tools_to_google_declarations

    tools = [
        {
            "type": "function",
            "function": {"name": "tool_a", "description": "A"},
        },
        {
            "type": "function",
            "function": {"name": "tool_b", "description": "B"},
        },
    ]
    decls = aura_tools_to_google_declarations(tools)
    assert len(decls) == 2
    assert {d["name"] for d in decls} == {"tool_a", "tool_b"}


def test_tool_mapping_skips_malformed():
    from aura.providers.google_cloud.mapping import aura_tools_to_google_declarations

    tools = [
        {"type": "function", "function": {"name": "good"}},
        {"type": "function"},  # no function dict → skipped
        {"type": "unknown"},  # wrong type, no function dict → skipped
        {"function": {"name": "also_valid"}},  # has function key → included
    ]
    decls = aura_tools_to_google_declarations(tools)
    assert len(decls) == 2
    names = {d["name"] for d in decls}
    assert names == {"good", "also_valid"}


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------


def test_message_mapping_system():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    system_inst, contents = aura_messages_to_google_contents(
        [{"role": "system", "content": "You are helpful."}]
    )
    assert system_inst == "You are helpful."
    assert contents == []


def test_message_mapping_multiple_system():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    system_inst, contents = aura_messages_to_google_contents(
        [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
        ]
    )
    assert "First." in system_inst
    assert "Second." in system_inst


def test_message_mapping_user():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    _, contents = aura_messages_to_google_contents(
        [{"role": "user", "content": "hello"}]
    )
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "hello"


def test_message_mapping_tool_calls():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    _, contents = aura_messages_to_google_contents(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "test.py"}',
                        },
                    }
                ],
            }
        ]
    )
    assert len(contents) == 1
    assert contents[0]["role"] == "model"
    parts = contents[0]["parts"]
    fc_parts = [p for p in parts if "function_call" in p]
    assert len(fc_parts) == 1
    assert fc_parts[0]["function_call"]["name"] == "read_file"
    assert fc_parts[0]["function_call"]["args"] == {"path": "test.py"}


def test_message_mapping_tool_result():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    _, contents = aura_messages_to_google_contents(
        [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "file contents",
            }
        ]
    )
    assert len(contents) == 1
    parts = contents[0]["parts"]
    fr_parts = [p for p in parts if "function_response" in p]
    assert len(fr_parts) == 1
    assert fr_parts[0]["function_response"]["name"] == "read_file"
    assert fr_parts[0]["function_response"]["response"]["result"] == "file contents"


def test_message_mapping_combined():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Read test.py"},
        {
            "role": "assistant",
            "content": "Let me read that.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "test.py"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "read_file",
            "content": "print('hello')",
        },
    ]
    system_inst, contents = aura_messages_to_google_contents(messages)
    assert system_inst == "Be helpful."
    assert len(contents) == 3
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
    assert contents[2]["role"] == "user"  # tool results become user role


def test_message_mapping_handles_reasoning_content():
    from aura.providers.google_cloud.mapping import aura_messages_to_google_contents

    _, contents = aura_messages_to_google_contents(
        [
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "thinking...",
            }
        ]
    )
    parts = contents[0]["parts"]
    texts = [p["text"] for p in parts if "text" in p]
    assert "thinking..." in texts
    assert "answer" in texts


# ---------------------------------------------------------------------------
# Provider registry (conditional visibility)
# ---------------------------------------------------------------------------


def test_provider_hidden_without_flag(monkeypatch):
    """Without AURA_GOOGLE_CLOUD_PROVIDER, google_cloud is not in registry."""
    monkeypatch.delenv("AURA_GOOGLE_CLOUD_PROVIDER", raising=False)
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    reg = aura.providers.registry.provider_registry
    ids = reg.ids()
    assert "google_cloud" not in ids
    assert set(ids) == {"deepseek", "openai", "openrouter", "anthropic"}


def test_provider_visible_with_flag(monkeypatch):
    """With AURA_GOOGLE_CLOUD_PROVIDER=1, google_cloud is in registry."""
    monkeypatch.setenv("AURA_GOOGLE_CLOUD_PROVIDER", "1")
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    reg = aura.providers.registry.provider_registry
    ids = reg.ids()
    assert "google_cloud" in ids
    assert len(ids) == 5


def test_existing_providers_still_work_with_flag(monkeypatch):
    """Existing 4 providers still present when flag is on."""
    monkeypatch.setenv("AURA_GOOGLE_CLOUD_PROVIDER", "1")
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    reg = aura.providers.registry.provider_registry
    ids = set(reg.ids())
    assert ids.issuperset({"deepseek", "openai", "openrouter", "anthropic"})


def test_registry_create_client_returns_google_cloud_client(monkeypatch):
    """With flag, create_client('google_cloud') returns GoogleCloudClient."""
    monkeypatch.setenv("AURA_GOOGLE_CLOUD_PROVIDER", "1")
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    reg = aura.providers.registry.provider_registry

    from aura.providers.google_cloud.client import GoogleCloudClient

    client = reg.create_client("google_cloud")
    assert isinstance(client, GoogleCloudClient)


def test_registry_create_client_still_works_for_deepseek():
    """create_client for existing providers still returns DeepSeekClient."""
    import aura.providers.registry
    from aura.client.deepseek import DeepSeekClient

    reg = aura.providers.registry.provider_registry
    client = reg.create_client("deepseek")
    assert isinstance(client, DeepSeekClient)


# ---------------------------------------------------------------------------
# Google Cloud Client (offline)
# ---------------------------------------------------------------------------


def test_google_cloud_client_construction():
    """Constructing client doesn't make API calls (lazy init)."""
    from aura.providers.google_cloud.client import GoogleCloudClient

    client = GoogleCloudClient(project="test", location="us-central1")
    assert client._client is None  # Not initialized yet
    assert client._project == "test"
    assert client._location == "us-central1"


def test_google_cloud_client_list_models_handles_import_error(monkeypatch):
    """list_models returns [] when google-genai is not installed."""
    from aura.providers.google_cloud.client import GoogleCloudClient

    client = GoogleCloudClient()
    # _get_client will try to import google.genai which isn't installed
    # but list_models catches Exception and returns []
    result = client.list_models()
    assert result == []


def test_google_cloud_client_stream_handles_import_error(monkeypatch):
    """stream yields ApiError when google-genai is not installed."""
    from aura.client.events import ApiError
    from aura.providers.google_cloud.client import GoogleCloudClient

    # Force _get_client to raise ImportError regardless of whether google-genai
    # is actually installed.
    def _raise_import_error(self):
        raise ImportError("No module named 'google.genai'")

    monkeypatch.setattr(
        "aura.providers.google_cloud.client.GoogleCloudClient._get_client",
        _raise_import_error,
    )

    client = GoogleCloudClient()
    events = list(
        client.stream(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="gemini-2.0-flash-001",
            thinking="off",
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], ApiError)
    assert "google.genai" in events[0].message.lower()


def test_no_live_api_call_on_import():
    """Importing the module does not call any APIs."""
    _unload_google_cloud_modules()
    # This should not raise or hang
    import aura.providers.google_cloud  # noqa: F401


# ---------------------------------------------------------------------------
# Google Cloud provider spec
# ---------------------------------------------------------------------------


def test_google_cloud_provider_spec(monkeypatch):
    """Verify the ProviderSpec for google_cloud has correct shape."""
    monkeypatch.setenv("AURA_GOOGLE_CLOUD_PROVIDER", "1")
    _unload_google_cloud_modules()
    import aura.providers.catalog
    import aura.providers.registry

    importlib.reload(aura.providers.catalog)
    importlib.reload(aura.providers.registry)
    reg = aura.providers.registry.provider_registry

    spec = reg.get("google_cloud")
    assert spec.label == "Google Cloud Gemini"
    assert spec.base_url == ""
    assert spec.env_key == "GOOGLE_CLOUD_PROJECT"
    assert spec.default_model == "gemini-2.0-flash-001"
    assert spec.default_thinking == "off"
    assert "gemini-2.0-flash-001" in spec.models
    assert "gemini-2.5-flash-001" in spec.models
    assert "gemini-2.5-pro-001" in spec.models


# ---------------------------------------------------------------------------
# ENSURE_NO_RAW_BYTES alias
# ---------------------------------------------------------------------------


def test_ensure_no_raw_bytes_is_make_message_json_safe():
    from aura.providers.google_cloud.mapping import ENSURE_NO_RAW_BYTES
    from aura.providers.google_cloud.signatures import make_message_json_safe

    assert ENSURE_NO_RAW_BYTES is make_message_json_safe
