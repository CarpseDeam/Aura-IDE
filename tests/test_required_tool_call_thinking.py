"""``tool_choice="required"`` and DeepSeek thinking are mutually exclusive.

DeepSeek answers a request that carries both with ``400: Thinking mode does not
support this tool_choice``.  Aura issues exactly two requests that pin
``tool_choice="required"`` — the decision checkpoint and the focused action —
and the requirement is the load-bearing half of both: the checkpoint is answered
with a control call or it is reissued.  So thinking is what gives way.

Asserted here, at both levels:

* the send loop sends the decision checkpoint and the focused action with
  thinking off, while every ordinary discovery, recovery, validation, and final
  request still carries the user's selection;
* the DeepSeek client refuses to transmit the forbidden pair regardless of what
  the caller passed, using the ordinary off-mode payload and logging both the
  requested and the effective mode;
* neither the requirement nor any other provider's behaviour moves.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest

from aura.client.deepseek import DeepSeekClient
from aura.conversation.focused_action import (
    DECISION_CHECKPOINT_THINKING,
    FOCUSED_ACTION_THINKING,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    KIND_CHECKPOINT,
    KIND_FOCUSED,
    SELECTED_THINKING,
    Recorder,
    ScriptedBackend,
    build_manager,
    commit_round,
    final_round,
    make_workspace,
    read_round,
    run,
    write_round,
)


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


# ── the two narrowed requests ───────────────────────────────────────────────


class TestSendLoopThinkingPerRequest:
    """Which thinking mode each request shape actually leaves with."""

    @pytest.fixture
    def backend(self, tmp_path, isolated_streams) -> ScriptedBackend:
        workspace = make_workspace(tmp_path / "proj")
        scripted = ScriptedBackend([
            read_round("r0", 0),
            commit_round(),
            write_round(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, scripted.stream)
        run(build_manager(workspace), Recorder())
        return scripted

    def test_decision_checkpoint_disables_thinking(self, backend) -> None:
        checkpoints = backend.checkpoint_calls()
        assert checkpoints, "the turn must have reached a decision checkpoint"
        for call in checkpoints:
            assert call["thinking"] == "off"
            assert call["thinking"] == DECISION_CHECKPOINT_THINKING

    def test_focused_action_still_disables_thinking(self, backend) -> None:
        actions = backend.action_calls()
        assert actions, "the turn must have reached a focused action"
        for call in actions:
            assert call["thinking"] == FOCUSED_ACTION_THINKING == "off"

    def test_every_other_request_keeps_the_user_selection(self, backend) -> None:
        others = [
            call
            for call, kind in zip(backend.calls, backend.request_kinds())
            if kind not in (KIND_CHECKPOINT, KIND_FOCUSED)
        ]
        assert others, "the turn must have issued ordinary requests too"
        assert SELECTED_THINKING != "off", "the fixture must not hide a regression"
        for call in others:
            assert call["thinking"] == SELECTED_THINKING

    def test_the_requirement_itself_is_not_weakened(self, backend) -> None:
        """Thinking gave way; ``require_tool_call`` did not."""
        narrowed = backend.checkpoint_calls() + backend.action_calls()
        assert narrowed
        for call in narrowed:
            assert call["require_tool_call"] is True


# ── the transport-level invariant ───────────────────────────────────────────


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any):
        self.kwargs = kwargs
        return iter(())


class _FakeOpenAI:
    def __init__(self) -> None:
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _FakeCompletions()


def _sent(
    provider: str,
    thinking: str,
    *,
    require_tool_call: bool,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drive the real client and return the kwargs it handed the provider."""
    client = DeepSeekClient(api_key="test-key", provider=provider)
    fake = _FakeOpenAI()
    client._client = fake  # noqa: SLF001 — transport seam
    tools = [{
        "type": "function",
        "function": {"name": "commit_implementation_decision", "parameters": {}},
    }]
    cancel = threading.Event()
    list(client.stream(
        messages=messages if messages is not None else [
            {"role": "user", "content": "hi"}
        ],
        tools=tools,
        model="deepseek-chat",
        thinking=thinking,  # type: ignore[arg-type]
        cancel_event=cancel,
        require_tool_call=require_tool_call,
    ))
    assert fake.chat.completions.kwargs is not None
    return fake.chat.completions.kwargs


class TestDeepSeekNeverTransmitsTheForbiddenPair:

    @pytest.mark.parametrize("thinking", ["high", "max", "auto"])
    def test_required_tool_call_forces_thinking_disabled(self, thinking: str) -> None:
        kwargs = _sent("deepseek", thinking, require_tool_call=True)

        assert kwargs["tool_choice"] == "required"
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in kwargs, (
            "the off-mode request omits the key entirely — never sends a null"
        )

    def test_the_coerced_payload_is_the_ordinary_off_payload(self) -> None:
        """No second DeepSeek payload format was invented for this."""
        coerced = _sent("deepseek", "max", require_tool_call=True)
        genuine = _sent("deepseek", "off", require_tool_call=True)

        assert coerced == genuine

    def test_off_selection_is_untouched(self) -> None:
        kwargs = _sent("deepseek", "off", require_tool_call=True)

        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert kwargs["tool_choice"] == "required"

    @pytest.mark.parametrize("thinking", ["high", "max", "auto"])
    def test_ordinary_requests_keep_thinking_enabled(self, thinking: str) -> None:
        kwargs = _sent("deepseek", thinking, require_tool_call=False)

        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        assert kwargs["tool_choice"] == "auto"

    def test_both_modes_are_logged_when_the_coercion_applies(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="aura.client.deepseek"):
            _sent("deepseek", "high", require_tool_call=True)

        text = caplog.text
        assert "deepseek_thinking_coerced_for_required_tool_call" in text
        assert "requested_thinking=high" in text
        assert "effective_thinking=off" in text

    def test_nothing_is_logged_when_it_does_not_apply(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="aura.client.deepseek"):
            _sent("deepseek", "high", require_tool_call=False)

        assert "deepseek_thinking_coerced_for_required_tool_call" not in caplog.text

    @pytest.mark.parametrize("provider", ["openai", "openrouter"])
    def test_other_providers_are_not_touched(self, provider: str) -> None:
        """The defect is the DeepSeek request; nobody else loses their thinking."""
        kwargs = _sent(provider, "high", require_tool_call=True)

        assert kwargs["tool_choice"] == "required"
        assert kwargs["reasoning_effort"] == "high"
        assert "extra_body" not in kwargs


# ── replaying reasoning_content back to a thinking-enabled request ──────────
#
# The other half of the same DeepSeek rule: with thinking on, every assistant
# message after the last user message must carry ``reasoning_content``, or the
# request is rejected with 400 "The `reasoning_content` in the thinking mode
# must be passed back to the API".  Aura honestly produces messages without it —
# the two narrowed protocol requests run with thinking off, workers synthesize
# assistant turns, and a reloaded conversation can predate the selection.


def _chain(*, checkpoint_reasoning: str | None = None) -> list[dict[str, Any]]:
    """A real production chain: one thinking round, then a thinking-off one."""
    checkpoint: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {
                "name": "commit_implementation_decision", "arguments": "{}",
            },
        }],
    }
    if checkpoint_reasoning is not None:
        checkpoint["reasoning_content"] = checkpoint_reasoning
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Update notes.md."},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should read the file first.",
            "tool_calls": [{
                "id": "r1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "r1", "content": "{}"},
        checkpoint,
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]


def _assistants_after_last_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    boundary = max(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1
    )
    return [
        m for m in messages[boundary + 1:] if m.get("role") == "assistant"
    ]


class TestReasoningContentIsReplayed:

    @pytest.mark.parametrize("thinking", ["high", "max", "auto"])
    def test_every_assistant_in_the_active_chain_carries_reasoning(
        self, thinking: str
    ) -> None:
        kwargs = _sent(
            "deepseek", thinking, require_tool_call=False, messages=_chain()
        )

        chain = _assistants_after_last_user(kwargs["messages"])
        assert chain, "the fixture must exercise a non-empty active chain"
        for msg in chain:
            assert msg.get("reasoning_content"), (
                "DeepSeek rejects a thinking-mode replay with a hole in the chain"
            )

    @pytest.mark.parametrize("thinking", ["high", "max", "auto"])
    def test_real_reasoning_is_never_overwritten(self, thinking: str) -> None:
        original = _chain(checkpoint_reasoning="The decision is made.")
        kwargs = _sent(
            "deepseek", thinking, require_tool_call=False, messages=original
        )

        assert [m.get("reasoning_content") for m in kwargs["messages"]] == [
            m.get("reasoning_content") for m in original
        ]

    def test_messages_before_the_boundary_keep_shedding_reasoning(self) -> None:
        """Only the chain is filled in — the token savings elsewhere are kept."""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "an answer with no reasoning"},
            {"role": "user", "content": "second"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "r1", "content": "{}"},
        ]
        kwargs = _sent(
            "deepseek", "high", require_tool_call=False, messages=messages
        )

        sent = kwargs["messages"]
        assert "reasoning_content" not in sent[1], (
            "an assistant before the last user message may omit it"
        )
        assert sent[3]["reasoning_content"]

    def test_the_callers_history_is_not_mutated(self) -> None:
        messages = _chain()
        before = json.dumps(messages, sort_keys=True)

        _sent("deepseek", "high", require_tool_call=False, messages=messages)

        assert json.dumps(messages, sort_keys=True) == before

    def test_thinking_off_sends_the_chain_untouched(self) -> None:
        """Nothing to satisfy, so nothing is added."""
        messages = _chain()
        kwargs = _sent(
            "deepseek", "off", require_tool_call=False, messages=messages
        )

        assert kwargs["messages"] == messages

    def test_a_coerced_protocol_request_pays_nothing(self) -> None:
        """The request that coerced to off is a genuine off-mode request."""
        messages = _chain()
        kwargs = _sent(
            "deepseek", "high", require_tool_call=True, messages=messages
        )

        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert kwargs["messages"] == messages

    @pytest.mark.parametrize("provider", ["openai", "openrouter"])
    def test_other_providers_are_not_given_placeholders(
        self, provider: str
    ) -> None:
        messages = _chain()
        kwargs = _sent(
            provider, "high", require_tool_call=False, messages=messages
        )

        assert kwargs["messages"] == messages

    def test_the_fill_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="aura.client.deepseek"):
            _sent("deepseek", "high", require_tool_call=False, messages=_chain())

        assert "deepseek_reasoning_replay_filled" in caplog.text
        assert "messages_filled=1" in caplog.text

    def test_nothing_is_logged_when_the_chain_is_already_whole(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="aura.client.deepseek"):
            _sent(
                "deepseek", "high", require_tool_call=False,
                messages=_chain(checkpoint_reasoning="The decision is made."),
            )

        assert "deepseek_reasoning_replay_filled" not in caplog.text
