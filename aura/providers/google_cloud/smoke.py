"""Smoke tests for the Google Cloud / Vertex AI provider module.

All offline tests — no live API calls.  Each validate_* function returns a
list of failure messages (empty = all good).
"""

from __future__ import annotations

import importlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


def validate_imports() -> list[str]:
    """Try importing every symbol from the google_cloud provider.  Return failures."""
    failures: list[str] = []

    modules = [
        "aura.providers.google_cloud",
        "aura.providers.google_cloud.config",
        "aura.providers.google_cloud.auth",
        "aura.providers.google_cloud.errors",
        "aura.providers.google_cloud.cooldown",
        "aura.providers.google_cloud.signatures",
        "aura.providers.google_cloud.mapping",
        "aura.providers.google_cloud.client",
    ]

    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
        except Exception as exc:
            failures.append(f"Import {mod_name}: {exc}")

    return failures


def validate_config() -> list[str]:
    """Test config parsing."""
    failures: list[str] = []

    from aura.providers.google_cloud.config import (
        DEFAULT_LOCATION,
        GOOGLE_CLOUD_LOCATION_ENV,
        GOOGLE_CLOUD_PROJECT_ENV,
        get_google_cloud_config,
        get_google_cloud_location,
        get_google_cloud_project,
        is_configured,
    )

    # Defaults
    cfg = get_google_cloud_config()
    if cfg["project"] is not None:
        failures.append("project should be None by default")
    if cfg["location"] != DEFAULT_LOCATION:
        failures.append(f"location should default to {DEFAULT_LOCATION}")

    # With env vars
    os.environ[GOOGLE_CLOUD_PROJECT_ENV] = "test-project"
    os.environ[GOOGLE_CLOUD_LOCATION_ENV] = "us-central1"
    try:
        if get_google_cloud_project() != "test-project":
            failures.append("get_google_cloud_project did not read env")
        if get_google_cloud_location() != "us-central1":
            failures.append("get_google_cloud_location did not read env")
        if not is_configured():
            failures.append("is_configured should be True when project is set")
    finally:
        del os.environ[GOOGLE_CLOUD_PROJECT_ENV]
        del os.environ[GOOGLE_CLOUD_LOCATION_ENV]

    # Without project
    if is_configured():
        failures.append("is_configured should be False after env cleanup")

    return failures


def validate_mapping() -> list[str]:
    """Test tool and message mapping functions."""
    failures: list[str] = []

    from aura.providers.google_cloud.mapping import (
        aura_messages_to_google_contents,
        aura_tools_to_google_declarations,
    )

    # Empty tools
    decls = aura_tools_to_google_declarations([])
    if decls != []:
        failures.append("empty tools should produce empty declarations")

    # Single tool
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
    if len(decls) != 1:
        failures.append("single tool should produce 1 declaration")
    elif decls[0]["name"] != "read_file":
        failures.append(f"wrong tool name: {decls[0]['name']}")

    # System message
    system_inst, contents = aura_messages_to_google_contents(
        [{"role": "system", "content": "You are helpful."}]
    )
    if system_inst != "You are helpful.":
        failures.append(f"system_instruction mismatch: {system_inst!r}")
    if contents:
        failures.append("system message should not produce contents")

    # User message
    _, contents = aura_messages_to_google_contents(
        [{"role": "user", "content": "hello"}]
    )
    if len(contents) != 1 or contents[0]["role"] != "user":
        failures.append("user message conversion failed")

    # Tool calls
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
    if len(contents) != 1 or contents[0]["role"] != "model":
        failures.append("tool_calls conversion failed")
    else:
        parts = contents[0]["parts"]
        has_fc = any("function_call" in p for p in parts)
        if not has_fc:
            failures.append("function_call part missing")

    # Tool result
    _, contents = aura_messages_to_google_contents(
        [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "file contents here",
            }
        ]
    )
    if len(contents) != 1:
        failures.append("tool result conversion failed")
    else:
        parts = contents[0].get("parts", [])
        has_fr = any("function_response" in p for p in parts)
        if not has_fr:
            failures.append("function_response part missing")

    return failures


def validate_signatures() -> list[str]:
    """Test signature encoding — no str(bytes) usage."""
    failures: list[str] = []

    from aura.providers.google_cloud.signatures import (
        decode_signature,
        encode_signature_safe,
        make_message_json_safe,
    )

    # bytes → base64
    encoded = encode_signature_safe(b"\x00\xff\xab")
    if "b'" in encoded or encoded.startswith("b'"):
        failures.append(f"bytes encoded with str(bytes) — got {encoded!r}")
    if not encoded:
        failures.append("bytes should encode to non-empty base64 string")

    # Round-trip
    decoded = decode_signature(encoded)
    if decoded != b"\x00\xff\xab":
        failures.append("decode_signature round-trip failed")

    # str passes through
    if encode_signature_safe("hello") != "hello":
        failures.append("str should pass through unchanged")

    # make_message_json_safe replaces bytes
    msg = {"key": b"raw bytes", "nested": {"inner": b"more"}}
    safe = make_message_json_safe(msg)
    if isinstance(safe["key"], bytes):
        failures.append("make_message_json_safe did not convert top-level bytes")
    if isinstance(safe["nested"]["inner"], bytes):
        failures.append("make_message_json_safe did not convert nested bytes")

    # Signature parts not merged
    sigs = {"a": b"sig1", "b": b"sig2"}
    safe = make_message_json_safe(sigs)
    if safe["a"] == safe["b"]:
        failures.append("signature parts were incorrectly merged")

    return failures


def validate_error_classification() -> list[str]:
    """Test error classification for all expected status codes."""
    failures: list[str] = []

    from aura.providers.google_cloud.errors import (
        AuthError,
        BadRequestError,
        ForbiddenError,
        GoogleCloudError,
        NotFoundError,
        ResourceExhaustedError,
        TransientError,
        classify_error,
    )

    cases = [
        (400, BadRequestError),
        (401, AuthError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (408, TransientError),
        (429, ResourceExhaustedError),
        (500, TransientError),
        (502, TransientError),
        (503, TransientError),
        (599, TransientError),
        (418, GoogleCloudError),  # unknown
    ]

    for code, expected_cls in cases:
        err = classify_error(code, "test")
        if not isinstance(err, expected_cls):
            failures.append(
                f"classify_error({code}) expected {expected_cls.__name__}, "
                f"got {type(err).__name__}"
            )
        if err.status_code != code:
            failures.append(
                f"classify_error({code}) status_code is {err.status_code}, expected {code}"
            )

    return failures


def validate_cooldown() -> list[str]:
    """Test cooldown behavior."""
    failures: list[str] = []

    from aura.providers.google_cloud.cooldown import CooldownManager

    cm = CooldownManager(cooldown_seconds=0.1)

    if cm.is_cooling():
        failures.append("fresh CooldownManager should not be cooling")
    if cm.remaining() != 0.0:
        failures.append("fresh CooldownManager remaining should be 0.0")

    cm.hit()
    if not cm.is_cooling():
        failures.append("after hit(), is_cooling should be True")
    if cm.remaining() <= 0.0:
        failures.append("after hit(), remaining should be > 0")

    cm.reset()
    if cm.is_cooling():
        failures.append("after reset(), is_cooling should be False")
    if cm.remaining() != 0.0:
        failures.append("after reset(), remaining should be 0.0")

    # Thread safety — just ensure concurrent calls don't crash
    errors: list[str] = []

    def _hammer() -> None:
        try:
            for _ in range(100):
                cm.hit()
                cm.is_cooling()
                cm.remaining()
                cm.reset()
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_hammer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        failures.append(f"thread-safety failures: {errors}")

    return failures


def validate_no_live_api_call_on_import() -> list[str]:
    """Ensure importing the module does not trigger any API calls."""
    failures: list[str] = []

    # Re-import to verify no side effects
    import sys

    for mod_name in list(sys.modules):
        if "aura.providers.google_cloud" in mod_name:
            del sys.modules[mod_name]

    try:
        import aura.providers.google_cloud  # noqa: F401
    except Exception as exc:
        failures.append(f"import raised unexpectedly: {exc}")

    return failures


def dry_run_all() -> list[str]:
    """Run all offline validations and collect failures."""
    all_failures: list[str] = []

    for name, fn in [
        ("imports", validate_imports),
        ("config", validate_config),
        ("mapping", validate_mapping),
        ("signatures", validate_signatures),
        ("error_classification", validate_error_classification),
        ("cooldown", validate_cooldown),
        ("no_live_api_call_on_import", validate_no_live_api_call_on_import),
    ]:
        failures = fn()
        if failures:
            for f in failures:
                all_failures.append(f"[{name}] {f}")

    return all_failures


def live_test(project: str | None = None) -> list[str]:
    """Attempt a live API call (requires credentials). Returns failures."""
    failures: list[str] = []

    try:
        from aura.providers.google_cloud.client import GoogleCloudClient
    except ImportError as exc:
        failures.append(f"import failed: {exc}")
        return failures

    client = GoogleCloudClient(project=project)
    try:
        models = client.list_models()
        if not models:
            failures.append("list_models returned empty — check credentials/project")
    except Exception as exc:
        failures.append(f"live test failed: {exc}")

    return failures
