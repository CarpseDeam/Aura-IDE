"""One-shot DeepSeek Responses API protocol probe.

This script intentionally bypasses Aura's provider, history, and conversation
layers. It makes at most two non-streaming requests with SDK retries disabled:
the first must return exactly one expected function call, and the second sends
that call plus a canned function result without any prior reasoning item.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

API_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"
FUNCTION_NAME = "get_probe_value"
FUNCTION_KEY = "aura-probe"
CANNED_OUTPUT = "AURA_RESPONSES_PROBE_7F3C"

INSTRUCTIONS = (
    "You are a protocol test assistant. Use the provided function exactly once "
    "when requested, and do not answer from your own knowledge."
)
USER_PROMPT = (
    'Call get_probe_value with exactly the key "aura-probe". '
    "Do not call any other tool and do not provide a final answer before calling it."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": FUNCTION_NAME,
    "description": "Return the probe value for a key without performing side effects.",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
}


def to_jsonable(value: Any) -> Any:
    """Convert SDK/Pydantic objects to JSON-safe values without API secrets."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json", exclude_none=False))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def sanitize(value: Any, api_key: str) -> Any:
    """Replace the configured key wherever an SDK error might echo it."""
    value = to_jsonable(value)
    if isinstance(value, str):
        return value.replace(api_key, "<redacted-api-key>") if api_key else value
    if isinstance(value, list):
        return [sanitize(item, api_key) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item, api_key) for key, item in value.items()}
    return value


def build_first_request() -> dict[str, Any]:
    """Build the exact first request body, with no tool choice override."""
    return {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": [{"role": "user", "content": USER_PROMPT}],
        "reasoning": {"effort": "max"},
        "tools": [dict(TOOL_SCHEMA)],
        "stream": False,
    }


def build_second_request(function_call: Mapping[str, Any]) -> dict[str, Any]:
    """Build a stateless continuation from the exact returned call item."""
    call_id = function_call.get("call_id")
    return {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": [
            {"role": "user", "content": USER_PROMPT},
            dict(function_call),
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": CANNED_OUTPUT,
            },
        ],
        "reasoning": {"effort": "max"},
        "tools": [dict(TOOL_SCHEMA)],
        "stream": False,
    }


def output_items(response: Any) -> list[dict[str, Any]]:
    """Return serialized output items from an SDK response."""
    raw_items = getattr(response, "output", None)
    if raw_items is None and isinstance(response, Mapping):
        raw_items = response.get("output", [])
    return [to_jsonable(item) for item in (raw_items or [])]


def validate_expected_function_call(
    items: list[Mapping[str, Any]],
) -> tuple[bool, str, dict[str, Any] | None]:
    """Accept only one expected function call, allowing preceding reasoning."""
    non_reasoning = [item for item in items if item.get("type") != "reasoning"]
    if len(non_reasoning) != 1:
        return False, "first response did not contain exactly one non-reasoning item", None
    function_call = non_reasoning[0]
    if function_call.get("type") != "function_call":
        return False, "first response's non-reasoning item was not a function_call", None
    if function_call.get("name") != FUNCTION_NAME:
        return False, "function_call name was not get_probe_value", None
    call_id = function_call.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return False, "function_call did not contain a call_id", None
    arguments = function_call.get("arguments")
    try:
        parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return False, "function_call arguments were not valid JSON", None
    if parsed_arguments != {"key": FUNCTION_KEY}:
        return False, "function_call arguments were not exactly the probe key", None
    return True, "expected function_call returned", dict(function_call)


def contains_reasoning_item(items: Any) -> bool:
    """Detect prior reasoning items/content in an input item tree."""
    if isinstance(items, dict):
        if items.get("type") == "reasoning" or "reasoning_content" in items:
            return True
        return any(contains_reasoning_item(value) for value in items.values())
    if isinstance(items, list):
        return any(contains_reasoning_item(value) for value in items)
    return False


def response_text(response: Any, items: list[Mapping[str, Any]]) -> str:
    """Extract final assistant text from the SDK convenience field/output."""
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, Mapping) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def error_record(exc: Exception, api_key: str) -> dict[str, Any]:
    """Capture sanitized HTTP/API details without serializing credentials."""
    return {
        "exception_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "message": sanitize(str(exc), api_key),
        "body": sanitize(getattr(exc, "body", None), api_key),
        "response": sanitize(getattr(exc, "response", None), api_key),
    }


def response_record(response: Any, api_key: str) -> dict[str, Any]:
    """Capture the complete sanitized response and usage/cache structures."""
    items = output_items(response)
    usage = getattr(response, "usage", None)
    return {
        "output_item_types": [item.get("type") for item in items],
        "output_items": sanitize(items, api_key),
        "usage": sanitize(usage, api_key),
        "full_sdk_response": sanitize(response, api_key),
    }


def write_artifact(artifact_dir: Path, record: Mapping[str, Any]) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "probe.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_probe() -> int:
    started_at = datetime.now().astimezone()
    artifact_dir = Path(tempfile.gettempdir()) / (
        "Aura-DeepSeek-Responses-Probe-" + started_at.strftime("%Y%m%d-%H%M%S-%f")
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    record: dict[str, Any] = {
        "probe": {
            "started_at": started_at.isoformat(),
            "base_url": API_BASE_URL,
            "model": MODEL,
            "max_retries": 0,
            "returned_tool_executed": False,
        },
        "requests": {},
        "responses": {},
        "errors": {},
    }

    if not api_key:
        record["result"] = "inconclusive"
        record["errors"]["setup"] = {"message": "DEEPSEEK_API_KEY is not set"}
        record["elapsed_ms"] = 0
        write_artifact(artifact_dir, record)
        print(f"artifact_dir={artifact_dir}")
        print("result=inconclusive")
        print("error=DEEPSEEK_API_KEY is not set")
        return 2

    client = OpenAI(
        api_key=api_key,
        base_url=API_BASE_URL,
        max_retries=0,
        timeout=180.0,
    )
    total_started = time.perf_counter()

    first_request = build_first_request()
    record["requests"]["first"] = first_request
    first_started = time.perf_counter()
    try:
        first_response = client.responses.create(**first_request)
    except Exception as exc:  # noqa: BLE001
        record["errors"]["first"] = error_record(exc, api_key)
        record["timing"] = {"first_elapsed_ms": round((time.perf_counter() - first_started) * 1000, 2)}
        record["result"] = "inconclusive"
        record["elapsed_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        write_artifact(artifact_dir, record)
        print(f"artifact_dir={artifact_dir}")
        print("result=inconclusive")
        print(f"first_error={record['errors']['first']['exception_type']}")
        return 2

    first_items = output_items(first_response)
    record["responses"]["first"] = response_record(first_response, api_key)
    valid, validation_message, function_call = validate_expected_function_call(first_items)
    record["first_validation"] = {
        "accepted": valid,
        "message": validation_message,
        "function_call": sanitize(function_call, api_key),
    }
    record.setdefault("timing", {})["first_elapsed_ms"] = round(
        (time.perf_counter() - first_started) * 1000, 2
    )

    if not valid or function_call is None:
        record["result"] = "inconclusive"
        record["elapsed_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        write_artifact(artifact_dir, record)
        print(f"artifact_dir={artifact_dir}")
        print("result=inconclusive")
        print(f"first_output_types={record['responses']['first']['output_item_types']}")
        print(f"reason={validation_message}")
        return 2

    second_request = build_second_request(function_call)
    record["requests"]["second"] = second_request
    record["second_request_proof"] = {
        "contains_prior_reasoning_item": contains_reasoning_item(second_request["input"]),
        "input_item_types": [item.get("type", item.get("role")) for item in second_request["input"]],
        "previous_response_id_absent": "previous_response_id" not in second_request,
        "conversation_absent": "conversation" not in second_request,
    }
    second_started = time.perf_counter()
    try:
        second_response = client.responses.create(**second_request)
    except Exception as exc:  # noqa: BLE001
        record["errors"]["second"] = error_record(exc, api_key)
        record["timing"]["second_elapsed_ms"] = round((time.perf_counter() - second_started) * 1000, 2)
        record["result"] = "failure"
        record["elapsed_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        write_artifact(artifact_dir, record)
        print(f"artifact_dir={artifact_dir}")
        print("result=failure")
        print(f"second_error={record['errors']['second']['exception_type']}")
        return 1

    second_items = output_items(second_response)
    record["responses"]["second"] = response_record(second_response, api_key)
    final_content = response_text(second_response, second_items)
    record["final_continuation_content"] = final_content
    record["marker_found"] = CANNED_OUTPUT in final_content
    record["timing"]["second_elapsed_ms"] = round(
        (time.perf_counter() - second_started) * 1000, 2
    )
    record["elapsed_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
    record["result"] = (
        "success"
        if record["second_request_proof"]["contains_prior_reasoning_item"] is False
        and record["second_request_proof"]["previous_response_id_absent"]
        and record["second_request_proof"]["conversation_absent"]
        and record["marker_found"]
        else "failure"
    )
    write_artifact(artifact_dir, record)
    print(f"artifact_dir={artifact_dir}")
    print(f"result={record['result']}")
    print(f"request_one_output_types={record['responses']['first']['output_item_types']}")
    print(f"request_two_output_types={record['responses']['second']['output_item_types']}")
    print(f"request_two_contains_reasoning={record['second_request_proof']['contains_prior_reasoning_item']}")
    print(f"final_content={final_content}")
    print(f"elapsed_ms={record['elapsed_ms']}")
    return 0 if record["result"] == "success" else 1


if __name__ == "__main__":
    sys.exit(run_probe())
