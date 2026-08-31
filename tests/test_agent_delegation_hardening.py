"""Private transcript, cancellation, redaction, and telemetry regressions."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from aura.agents.delegation import DelegationResult, DelegationStatus
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.runtime import AgentDelegationRunner
from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import ApiError, ContentDelta, Done, ToolResult, Usage
from aura.conversation.agent_loop import AgentLoop, LoopStop
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.events import EventBus


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="reviewer000",
        scope=AgentScope.PROJECT,
        name="Reviewer",
        description="Reviews one focused change.",
        instructions="Report only demonstrated findings.",
    )


def _entry(permission: AgentPermission = AgentPermission.READ_ONLY) -> AgentRosterEntry:
    return AgentRosterEntry(_definition(), permission=permission)


def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _Backend:
    def __init__(self, rounds: list[list[object]]) -> None:
        self.rounds = rounds
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        yield from self.rounds[len(self.requests) - 1]


def _runner(tmp_path: Path, rounds: list[list[object]]) -> AgentDelegationRunner:
    backend = _Backend(rounds)
    return AgentDelegationRunner(
        workspace_root=tmp_path,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: backend,
    )


@pytest.fixture(autouse=True)
def configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda provider=None: True
    )


def test_only_terminal_child_round_survives_and_usage_sums_every_round(
    tmp_path: Path,
) -> None:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    tool_call = _call("read", "read_file", path="note.txt")
    runner = _runner(
        tmp_path,
        [
            [
                ContentDelta("private intermediate prose"),
                Usage(10, 2, 3, 7),
                Done(
                    "tool_calls",
                    {"role": "assistant", "content": "private intermediate prose", "tool_calls": [tool_call]},
                ),
            ],
            [
                ContentDelta("terminal answer"),
                Usage(20, 5, 4, 16),
                Done("stop", {"role": "assistant", "content": "terminal answer"}),
            ],
        ],
    )

    payload = runner.run(_entry(), "Read the note.").payload()

    assert payload["result"] == "terminal answer"
    assert "private intermediate prose" not in json.dumps(payload)
    assert payload["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 7,
        "cache_hit_tokens": 7,
        "cache_miss_tokens": 23,
    }


def test_empty_terminal_response_never_falls_back_to_tool_round_prose(
    tmp_path: Path,
) -> None:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    tool_call = _call("read", "read_file", path="note.txt")
    runner = _runner(
        tmp_path,
        [
            [
                ContentDelta("do not return this"),
                Done(
                    "tool_calls",
                    {"role": "assistant", "content": "do not return this", "tool_calls": [tool_call]},
                ),
            ],
            [Done("stop", {"role": "assistant", "content": ""})],
        ],
    )

    result = runner.run(_entry(), "Read the note.")

    assert result.status is DelegationStatus.FAILED
    assert result.result == ""
    assert "do not return this" not in json.dumps(result.payload())


def test_child_provider_errors_are_redacted_before_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "child-secret-token-12345"
    monkeypatch.setenv("CHILD_TEST_TOKEN", secret)
    runner = _runner(
        tmp_path,
        [[ApiError(500, f"provider rejected {secret}")]],
    )

    payload = runner.run(_entry(), "Review.").payload()

    assert secret not in json.dumps(payload)
    assert "[REDACTED]" in payload["error"]


def test_backend_raise_after_cancel_keeps_terminal_answer_and_usage(
    tmp_path: Path,
) -> None:
    cancel = threading.Event()
    cancel_before_done = False

    class _CancelledBackend:
        def stream(self, **_kwargs):
            yield Usage(30, 7, 4, 26)
            if cancel_before_done:
                cancel.set()
            yield Done("stop", {"role": "assistant", "content": "Stable answer."})
            if not cancel_before_done:
                cancel.set()
            raise RuntimeError("provider teardown failed")

    runner = AgentDelegationRunner(
        workspace_root=tmp_path,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: _CancelledBackend(),
    )

    completed = runner.run(_entry(), "Review.", cancel_event=cancel)

    assert completed.status is DelegationStatus.COMPLETED
    assert completed.result == "Stable answer."
    assert completed.failure_class == ""
    assert completed.usage is not None
    assert completed.usage.as_dict() == {
        "prompt_tokens": 30,
        "completion_tokens": 7,
        "cache_hit_tokens": 4,
        "cache_miss_tokens": 26,
    }

    cancel.clear()
    cancel_before_done = True
    cancelled = runner.run(_entry(), "Review again.", cancel_event=cancel)

    assert cancelled.status is DelegationStatus.CANCELLED
    assert cancelled.failure_class == "cancelled"


def test_api_error_after_transport_cancellation_preserves_terminal_truth(
    tmp_path: Path,
) -> None:
    cancel = threading.Event()
    done_first = False

    class _CancelledBackend:
        def stream(self, **_kwargs):
            if done_first:
                yield Done(
                    "stop", {"role": "assistant", "content": "Already complete."}
                )
            cancel.set()
            yield ApiError(499, "transport observed cancellation")

    runner = AgentDelegationRunner(
        workspace_root=tmp_path,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: _CancelledBackend(),
    )

    cancelled = runner.run(_entry(), "Review.", cancel_event=cancel)
    assert cancelled.status is DelegationStatus.CANCELLED

    cancel.clear()
    done_first = True
    completed = runner.run(_entry(), "Review again.", cancel_event=cancel)
    assert completed.status is DelegationStatus.COMPLETED
    assert completed.result == "Already complete."

    def callback_cancelled_outcome(event: Done | ApiError) -> tuple[LoopStop, History]:
        cancel.clear()
        history = History()
        history.append_user_text("Review callback ordering.")
        registry = ToolRegistry(tmp_path)
        loop = AgentLoop(
            history=history,
            stream=lambda **_kwargs: iter((event,)),
            tool_round=ToolRoundRunner(
                history=history,
                tools=registry,
                tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
            ),
        )

        def cancel_during_delivery(received: object) -> None:
            assert received is event
            cancel.set()

        outcome = loop.run(
            on_event=cancel_during_delivery,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=cancel,
            model="test-model",
            thinking="off",
            tool_defs=[],
        )
        return outcome.stop, history

    delivered_done = Done(
        "stop", {"role": "assistant", "content": "Delivered before callback cancel."}
    )
    done_stop, done_history = callback_cancelled_outcome(delivered_done)
    assert done_stop is LoopStop.COMPLETED
    assert done_history.messages[-1]["content"] == "Delivered before callback cancel."

    delivered_error = ApiError(500, "provider failed before callback cancel")
    error_stop, error_history = callback_cancelled_outcome(delivered_error)
    assert error_stop is LoopStop.API_ERROR
    assert [message["role"] for message in error_history.messages] == ["user"]


def test_cancelled_writable_result_and_change_set_remain_canonical(
    tmp_path: Path,
) -> None:
    history = History()
    roster = AgentTurnRoster(entries=(_entry(AgentPermission.READ_WRITE),))
    history.append_user_text("delegate", available_agent_ids=roster.ids)
    registry = ToolRegistry(tmp_path)
    registry.set_turn_agent_roster(roster)
    cancel = threading.Event()

    class CancellingRunner:
        def run(self, entry, task, *, cancel_event):
            assert cancel_event is cancel
            cancel_event.set()
            return DelegationResult(
                status=DelegationStatus.CANCELLED,
                agent_id=entry.agent_id,
                agent_name=entry.name,
                result="Stable partial report.",
                failure_class="cancelled",
                permission=AgentPermission.READ_WRITE.value,
                change_set_id="aw-cancelled-result",
                base_sha="a" * 40,
                result_sha="b" * 40,
                changed_paths=("partial.txt",),
            )

    registry.set_agent_delegation_runner(CancellingRunner())
    calls = [
        _call(
            "delegate-1",
            "delegate_agent",
            agent_id="reviewer000",
            task="Make the focused edit.",
        )
    ]
    history.append_assistant(
        {"role": "assistant", "content": None, "tool_calls": calls}
    )
    round_runner = ToolRoundRunner(
        history=history,
        tools=registry,
        tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
    )
    round_runner.begin_turn()

    outcome = round_runner.run(
        tool_calls=calls,
        on_event=lambda _event: None,
        approval_cb=lambda _request: ApprovalDecision(action="approve"),
        cancel_event=cancel,
        cleanup_cancelled=lambda _callback: history.repair_incomplete_tool_calls(),
        tool_defs=registry.tool_defs(),
    )

    tools = [message for message in history.messages if message["role"] == "tool"]
    assert outcome.cancelled is True
    assert len(tools) == 1
    payload = json.loads(tools[0]["content"])
    assert payload["status"] == "cancelled"
    assert payload["result"] == "Stable partial report."
    assert payload["change_set_id"] == "aw-cancelled-result"
    assert payload["changed_paths"] == ["partial.txt"]


def test_child_usage_is_emitted_once_from_the_paired_result() -> None:
    class ApprovalProxy:
        @staticmethod
        def consume_last_event():
            return None

    relay = ExecutionEventRelay(ApprovalProxy(), EventBus())
    observed: list[tuple] = []
    relay.delegationUsage.connect(lambda *args: observed.append(args))
    relay.relay(
        "root-run",
        ToolResult(
            tool_call_id="delegate-1",
            name="delegate_agent",
            ok=True,
            result="{}",
            extras={
                "delegation_provider": "openrouter",
                "delegation_model": "child-model",
                "delegation_usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 7,
                    "cache_hit_tokens": 7,
                    "cache_miss_tokens": 23,
                },
            },
        ),
    )
    relay.relay(
        "root-run",
        ToolResult(
            tool_call_id="delegate-without-usage",
            name="delegate_agent",
            ok=True,
            result="{}",
            extras={
                "delegation_provider": "openrouter",
                "delegation_model": "child-model",
            },
        ),
    )
    for call_id, usage in (
        ("delegate-empty-usage", {}),
        ("delegate-unrelated-usage", {"total_tokens": 37}),
        ("delegate-invalid-usage", {"prompt_tokens": "invalid"}),
        ("delegate-zero-usage", {"prompt_tokens": 0}),
    ):
        relay.relay(
            "root-run",
            ToolResult(
                tool_call_id=call_id,
                name="delegate_agent",
                ok=True,
                result="{}",
                extras={
                    "delegation_provider": "openrouter",
                    "delegation_model": "child-model",
                    "delegation_usage": usage,
                },
            ),
        )

    assert observed == [
        ("delegate-1", "openrouter", "child-model", 30, 7, 7, 23),
        ("delegate-zero-usage", "openrouter", "child-model", 0, 0, 0, 0),
    ]


def test_workflow_usage_emits_each_provider_model_group_in_order() -> None:
    class ApprovalProxy:
        @staticmethod
        def consume_last_event():
            return None

    relay = ExecutionEventRelay(ApprovalProxy(), EventBus())
    observed: list[tuple] = []
    relay.delegationUsage.connect(lambda *args: observed.append(args))
    relay.relay(
        "root-run",
        ToolResult(
            tool_call_id="workflow-1",
            name="run_agent_workflow",
            ok=True,
            result="{}",
            extras={
                "delegation_usage_groups": [
                    {
                        "provider": "deepseek",
                        "model": "step-model",
                        "prompt_tokens": 30,
                        "completion_tokens": 7,
                        "cache_hit_tokens": 7,
                        "cache_miss_tokens": 23,
                    },
                    {
                        "provider": "deepseek",
                        "model": "helper-model",
                        "prompt_tokens": 11,
                        "completion_tokens": 5,
                        "cache_hit_tokens": 1,
                        "cache_miss_tokens": 10,
                    },
                ]
            },
        ),
    )

    assert observed == [
        ("workflow-1", "deepseek", "step-model", 30, 7, 7, 23),
        ("workflow-1", "deepseek", "helper-model", 11, 5, 1, 10),
    ]
