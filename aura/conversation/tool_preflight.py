"""Structural and JSON-schema preflight for raw assistant tool calls.

Before any call in a round executes — before effect lookup, guard checks,
limits, approval, or dispatch — every raw tool call is validated here.  A
malformed call rejects the *whole batch* before execution, so an accepted
prefix can never run ahead of a call that vetoes the batch.

Two layers run in order:

1. **Structural** — the call envelope itself: the call is an object; ``id`` is
   a non-empty string unique within the batch; ``function`` is an object;
   ``name`` is a non-empty string; ``arguments`` is a JSON string whose
   decoded value is an object (never a list, string, number, boolean, or
   null).

2. **Schema** — decoded arguments are validated against the exposed tool's
   JSON schema, enforcing required fields, object/array/string/boolean/number/
   integer types, enums, numeric bounds, and ``additionalProperties: false``.

Nothing here coerces: ``"false"`` is not ``False``, ``1`` is not ``"1"``, a
numeric string is not a number, and a value is never forced through ``str()``.
``jsonschema`` is strict by construction.

An unknown or unexposed tool name is rejected here too: the name must be
non-empty *and* actually offered in the request that produced the call.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema


def decode_arguments(value: Any) -> tuple[Any | None, str | None]:
    """Return ``(decoded, error)`` for a raw ``arguments`` field.

    ``error`` is a human-readable message when the value is not a JSON string
    that decodes to a JSON object; ``decoded`` is ``None`` then.
    """
    if not isinstance(value, str):
        return None, "tool call arguments must be a JSON string"
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, f"failed to parse tool arguments as JSON: {exc}"
    if not isinstance(decoded, dict):
        return None, (
            "tool arguments must decode to a JSON object, got "
            f"{type(decoded).__name__}"
        )
    return decoded, None


def _safe_call_id(call: Any, index: int) -> str:
    if isinstance(call, dict):
        value = call.get("id")
        if isinstance(value, str) and value:
            return value
    return f"__malformed_call_{index}__"


def _safe_call_name(call: Any) -> str:
    if isinstance(call, dict):
        fn = call.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                return name
    return "<unknown>"


def structural_error(call: Any, index: int, seen_ids: dict[str, int]) -> dict[str, Any] | None:
    """Return an invalid-call dict when *call* violates the structural contract.

    ``seen_ids`` maps already-seen call ids to their index, for the batch
    uniqueness check.  Returns ``None`` when the call is structurally valid.
    """
    tool_call_id = _safe_call_id(call, index)
    name = _safe_call_name(call)

    if not isinstance(call, dict):
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": "tool call must be a JSON object",
        }
    if "id" not in call or not isinstance(call["id"], str) or not call["id"]:
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": "tool call id must be a non-empty string",
        }
    if call["id"] in seen_ids:
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": f"duplicate tool call id in batch: {call['id']}",
        }
    seen_ids[call["id"]] = index

    fn = call.get("function")
    if not isinstance(fn, dict):
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": "tool call function must be a JSON object",
        }
    raw_name = fn.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": "function name must be a non-empty string",
        }
    name = raw_name

    decoded, error = decode_arguments(fn.get("arguments"))
    if error is not None:
        return {
            "tool_call_id": tool_call_id,
            "name": name,
            "kind": "structure",
            "error": error,
        }
    return None


def preflight_structural(calls: list[Any]) -> dict[str, Any] | None:
    """Validate the structure of every call in *calls*; first invalid call or None."""
    seen_ids: dict[str, int] = {}
    for index, call in enumerate(calls):
        invalid = structural_error(call, index, seen_ids)
        if invalid is not None:
            return invalid
    return None


def exposed_tool_schemas(tool_defs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Map exposed tool names to their JSON ``parameters`` schema.

    A def without a usable ``parameters`` object validates against the
    default open ``{"type": "object"}`` schema, so a schema-less tool accepts
    any object — the structural layer already guarantees the value is an
    object.
    """
    result: dict[str, dict[str, Any]] = {}
    for tool_def in tool_defs or []:
        if not isinstance(tool_def, dict):
            continue
        fn = tool_def.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        parameters = fn.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object"}
        result[name] = parameters
    return result


def schema_errors(name: str, args: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors for *args* against *parameters*.

    Empty list means the arguments satisfy the schema.  ``jsonschema`` never
    coerces types, so a rejected call is reported exactly as submitted.
    """
    schema = parameters or {"type": "object"}
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for exc in validator.iter_errors(args):
        errors.append(f"{name} argument validation failed: {exc.message}")
        if len(errors) >= 3:
            break
    return errors


__all__ = [
    "decode_arguments",
    "exposed_tool_schemas",
    "preflight_structural",
    "schema_errors",
    "structural_error",
]
