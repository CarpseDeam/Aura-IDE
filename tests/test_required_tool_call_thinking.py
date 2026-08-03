"""Production SINGLE requests and DeepSeek thinking: the transport rules.

Two facts are asserted here.

First, at the send-loop level: production SINGLE is a conventional agent loop.
Every active request exposes the same stable catalog and the user-selected
thinking mode, and ``require_tool_call`` is never supplied — the narrowed
checkpoint/focused protocol requests are gone, so no request ever pins
``tool_choice="required"``.

Second, at the transport level: the DeepSeek client still refuses to transmit
the forbidden pair (``tool_choice="required"`` + thinking) for *any* legitimate
caller that does ask for it, and a thinking-enabled DeepSeek request still
replays ``reasoning_content`` on every assistant message after the last user
message.  Neither rule moved when the production loop stopped producing the
narrowed requests.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest

from aura.client.deepseek import (
    REASONING_REPLAY_PLACEHOLDER,
    DeepSeekClient,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    SELECTED_THINKING,
    Recorder,
    ScriptedBackend,
    build_manager,
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


# ── the send loop never narrows a request ───────────────────────────────────


class TestProductionRequestsUseTheOneNormalShape:
    """Every active SINGLE request keeps the stable catalog and the user's
    thinking selection; ``require_tool_call`` is never sent."""

    @pytest.fixture
    def backend(self, tmp_path, isolated_streams) -> ScriptedBackend:
        workspace = make_workspace(tmp_path / "proj")
        scripted = ScriptedBackend([
            read_round("r0", 0),
            write_round(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, scripted.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())
        return scripted

    def test_every_request_uses_the_user_selection(self, backend) -> None:
        assert SELECTED_THINKING != "off", "the fixture must not hide a regression"
        assert backend.every_request_thinking() == SELECTED_THINKING

    def test_require_tool_call_is_never_supplied(self, backend) -> None:
        assert backend.all_requests_stable() == [], backend.all_requests_stable()
        assert not any(
            backend.sent_require_tool_call(i) for i in range(len(backend.calls))
        )

    def test_the_stable_catalog_and_schema_hash_do_not_move(
        self, backend,
    ) -> None:
        """The active tool schema never alternates between ordinary, checkpoint,
        and focused-action catalogs."""
        violations = backend.all_requests_stable()
        assert violations == [], violations
        hashes = {backend.schema_hash(i) for i in range(len(backend.calls))}
        assert len(hashes) == 1, "the schema hash changed between requests"

    def test_canonical_history_has_no_request_only_placeholders(
        self, tmp_path, isolated_streams,
    ) -> None:
        """The DeepSeek reasoning-replay placeholder is a request-local fill; it
        never enters canonical history."""
        workspace = make_workspace(tmp_path / "proj2")
        scripted = ScriptedBackend([
            read_round("r0", 0),
            write_round(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, scripted.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())

        assert REASONING_REPLAY_PLACEHOLDER not in json.dumps(
            manager.history.messages
        )


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
        "function": {"name": "read_file", "parameters": {}},
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
# must be passed back to the API".  Aura honestly produces messages without it
# in places, so the transport fills the active chain request-locally.


def _chain(*, with_reasoning: str | None = None) -> list[dict[str, Any]]:
    """A real production chain: one thinking round, then one without reasoning."""
    read: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "r1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }
    if with_reasoning is not None:
        read["reasoning_content"] = with_reasoning
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Update notes.md."},
        read,
        {"role": "tool", "tool_call_id": "r1", "content": "{}"},
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
        original = _chain(with_reasoning="I should read the file first.")
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

    def test_a_coerced_required_tool_call_pays_nothing(self) -> None:
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
                messages=_chain(with_reasoning="I should read the file first."),
            )

        assert "deepseek_reasoning_replay_filled" not in caplog.text

    def test_canonical_history_never_receives_the_placeholder(self) -> None:
        """The placeholder is request-local: it exists only in the outbound
        transport copy, never in Aura's own history."""
        messages = _chain()
        _sent("deepseek", "high", require_tool_call=False, messages=messages)
        assert REASONING_REPLAY_PLACEHOLDER not in json.dumps(messages)
