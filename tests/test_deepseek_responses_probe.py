"""Offline contracts for the standalone DeepSeek Responses probe."""

from __future__ import annotations

from scripts.deepseek_responses_probe import (
    CANNED_OUTPUT,
    FUNCTION_NAME,
    build_first_request,
    build_second_request,
    contains_reasoning_item,
    validate_expected_function_call,
)


def test_first_request_is_non_streaming_max_and_has_no_tool_choice() -> None:
    request = build_first_request()

    assert request["model"] == "deepseek-v4-flash"
    assert request["reasoning"] == {"effort": "max"}
    assert request["stream"] is False
    assert "tool_choice" not in request
    assert request["tools"] == [{
        "type": "function",
        "name": FUNCTION_NAME,
        "description": "Return the probe value for a key without performing side effects.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    }]


def test_second_request_replays_exact_call_and_canned_output_without_reasoning() -> None:
    function_call = {
        "type": "function_call",
        "id": "fc_123",
        "call_id": "call_123",
        "name": FUNCTION_NAME,
        "arguments": '{"key":"aura-probe"}',
        "status": "completed",
    }

    request = build_second_request(function_call)

    assert request["input"][1] == function_call
    assert request["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": CANNED_OUTPUT,
    }
    assert contains_reasoning_item(request["input"]) is False
    assert "previous_response_id" not in request
    assert "conversation" not in request


def test_expected_call_validator_rejects_wrong_or_multiple_calls() -> None:
    expected = {
        "type": "function_call",
        "id": "fc_123",
        "call_id": "call_123",
        "name": FUNCTION_NAME,
        "arguments": '{"key":"aura-probe"}',
    }

    accepted, _, call = validate_expected_function_call([
        {"type": "reasoning", "id": "rs_123"},
        expected,
    ])
    assert accepted is True
    assert call == expected

    rejected, _, _ = validate_expected_function_call([
        expected,
        {**expected, "id": "fc_456", "call_id": "call_456"},
    ])
    assert rejected is False


def test_reasoning_detection_does_not_confuse_control_with_input_items() -> None:
    assert contains_reasoning_item({"effort": "max"}) is False
    assert contains_reasoning_item([{"type": "function_call_output"}]) is False
    assert contains_reasoning_item([{"type": "reasoning", "content": []}]) is True
    assert contains_reasoning_item([{"role": "assistant", "reasoning_content": "old"}]) is True
