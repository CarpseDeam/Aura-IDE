"""Structural and JSON-schema tool-call preflight hardening.

Before effect lookup, guard checks, limits, approval, or execution, every raw
tool call is validated:

* the call is an object; ``id`` is a non-empty, batch-unique string;
  ``function`` is an object; ``name`` is a non-empty string that is actually
  exposed in the request;
* ``arguments`` is a JSON string whose decoded value is an object — never a
  list, string, number, boolean, or null;
* decoded arguments satisfy the exposed tool's JSON schema: required fields,
  object/array/string/boolean/number/integer types, enums, bounds, and
  ``additionalProperties: false``;

A malformed call rejects the entire batch before execution, and every call —
the invalid one and each valid sibling — receives exactly one paired, factual
tool result.

Nothing here coerces: ``"false"`` is not ``False``, ``1`` is not ``"1"``, and
a numeric string is not a number.

Unexpected handler exceptions become a redacted ``internal_tool_error`` result
for that call instead of escaping the tool round.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry


def build_bundle(tmp_path):
    """Return (runner, history, workspace_root) with alpha.py present."""
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    history = History()
    history.set_system("You are Aura's production coding agent.")
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    runner = ToolRoundRunner(
        history=history,
        tools=tools,
        tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        planner_refresh=PlannerRefreshState(),
    )
    return runner, history, tmp_path


@pytest.fixture
def runner(tmp_path):
    return build_bundle(tmp_path)


def run_round(runner_bundle, calls):
    runner, history, _ = runner_bundle
    state = _SendState(mode="single", research_policy=None)
    runner.run(
        tool_calls=calls,
        state=state,
        on_event=lambda e: None,
        approval_cb=lambda req: None,
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda cb: None,
    )
    return history


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool"]


def _call(raw: Any) -> dict[str, Any]:
    return raw


def _read(call_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    args = {"path": "alpha.py"}
    if extra:
        args.update(extra)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps(args)},
    }


def _write(call_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps(args)},
    }


def _assert_malformed(history, call_index: int = 0) -> dict[str, Any]:
    results = tool_results(history.messages)
    assert len(results) >= call_index + 1, "every call must receive a paired result"
    payload = json.loads(results[call_index]["content"])
    assert payload["ok"] is False
    assert payload["failure_class"] == "tool_call_malformed"
    return payload


# ── malformed call structures ───────────────────────────────────────────────


class TestMalformedCallStructures:

    def test_non_object_call_rejects_the_whole_batch(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [
            _call("not-an-object"),
            _write("call-write", {"path": "new.py", "content": "x"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        results = tool_results(history.messages)
        assert len(results) == 2, "every call must receive exactly one paired result"
        malformed = json.loads(results[0]["content"])
        assert malformed["ok"] is False
        assert malformed["failure_class"] == "tool_call_malformed"
        sibling = json.loads(results[1]["content"])
        assert sibling["batch_rejected"] is True
        assert not (workspace / "new.py").exists(), "no call may execute"

    @pytest.mark.parametrize("raw_call", [
        {"function": {"name": "read_file", "arguments": "{}"}},
        {"id": "", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": 123, "function": {"name": "read_file", "arguments": "{}"}},
    ])
    def test_missing_or_invalid_id_rejects(self, tmp_path, raw_call: dict) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call(raw_call)]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        _assert_malformed(history)

    def test_duplicate_ids_reject_the_batch(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [
            _read("dup"),
            _write("dup", {"path": "new.py", "content": "x"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        results = tool_results(history.messages)
        assert len(results) == 2
        assert json.loads(results[0]["content"])["ok"] is False
        assert not (workspace / "new.py").exists()

    def test_function_not_an_object_rejects(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({"id": "c1", "function": "not-an-object"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        _assert_malformed(history)

    @pytest.mark.parametrize("bad_name", ["", None, 7])
    def test_empty_or_non_string_name_rejects(self, tmp_path, bad_name: Any) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {"name": bad_name, "arguments": "{}"},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        _assert_malformed(history)


class TestNonObjectArguments:

    @pytest.mark.parametrize("bad", ["[]", '"a string"', "42", "true", "null"])
    def test_non_object_arguments_reject(self, tmp_path, bad: str) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {"name": "read_file", "arguments": bad},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        _assert_malformed(history)

    def test_non_string_arguments_field_rejects(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {"name": "read_file", "arguments": {"path": "alpha.py"}},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        _assert_malformed(history)


# ── exposure and schema ─────────────────────────────────────────────────────


class TestExposureAndSchema:

    def test_an_unexposed_tool_name_rejects_the_batch(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        # dispatch_to_worker is a Planner tool: the single-agent catalog never
        # offers it, and unlike a superseded read it is not replay-callable.
        calls = [
            _call({
                "id": "call-bad",
                "type": "function",
                "function": {
                    "name": "dispatch_to_worker",
                    "arguments": json.dumps({
                        "goal": "g", "files": [], "spec": "s", "acceptance": "a",
                    }),
                },
            }),
            _write("call-write", {"path": "new.py", "content": "x"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        results = tool_results(history.messages)
        assert len(results) == 2
        bad = json.loads(results[0]["content"])
        assert bad["ok"] is False
        assert bad["failure_class"] == "tool_call_not_exposed"
        sibling = json.loads(results[1]["content"])
        assert sibling["batch_rejected"] is True
        assert not (workspace / "new.py").exists()

    def test_a_superseded_read_stays_callable_on_replay(self, tmp_path) -> None:
        """The single-agent catalog withholds the superseded reads to shape the
        model's choice, not to revoke them: their handlers stay registered so a
        replayed historical call still runs. Exposure preflight must honour
        that instead of rejecting the replay as an unknown tool."""
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "call-replay",
            "type": "function",
            "function": {"name": "read_files", "arguments": json.dumps({"paths": ["alpha.py"]})},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload.get("failure_class") != "tool_call_not_exposed"
        assert payload.get("ok") is not False

    def test_a_replayed_superseded_read_is_still_schema_checked(self, tmp_path) -> None:
        """Replay-callable is not schema-exempt."""
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "call-replay",
            "type": "function",
            "function": {"name": "read_files", "arguments": json.dumps({"paths": "alpha.py"})},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_a_nonexistent_tool_name_is_rejected_before_execution(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {"name": "no_such_tool", "arguments": "{}"},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_not_exposed"

    def test_boolean_is_not_an_integer(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_read("c1", {"offset": True})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_a_numeric_string_is_not_a_number(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_read("c1", {"offset": "10"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_missing_required_field_is_rejected(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        # write_file requires path and content.
        calls = [_write("c1", {"path": "new.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_additional_properties_are_rejected(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        # patch_file's parameters declare additionalProperties: false.
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {
                "name": "patch_file",
                "arguments": json.dumps({
                    "path": "alpha.py",
                    "edits": [{"old": "alpha", "new": "beta"}],
                    "bogus_key": 1,
                }),
            },
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_a_string_is_not_a_boolean(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_call({
            "id": "c1",
            "type": "function",
            "function": {"name": "grep_search", "arguments": json.dumps({"pattern": "x", "regex_mode": "false"})},
        })]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "tool_call_schema_violation"

    def test_well_formed_calls_still_execute(self, tmp_path) -> None:
        runner, history, workspace = build_bundle(tmp_path)
        calls = [_read("c1")]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)
        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is True
        assert "alpha = 1" in payload["content"]


# ── handler exceptions become redacted internal errors ─────────────────────


class TestHandlerExceptionContainment:

    def test_a_raising_handler_becomes_an_internal_tool_error_result(
        self, tmp_path, monkeypatch,
    ) -> None:
        runner, history, workspace = build_bundle(tmp_path)

        def boom(self, args, approval_cb, reject_all):
            raise RuntimeError("handler exploded with top-secret-credential-x")

        monkeypatch.setitem(TOOL_HANDLERS, "read_file", boom)
        calls = [_read("c1")]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round((runner, history, workspace), calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["ok"] is False
        assert payload["failure_class"] == "internal_tool_error"
        # The model-facing error must not leak the raw exception text.
        assert "top-secret-credential-x" not in payload["error"]
        assert "handler exploded" not in payload["error"]
