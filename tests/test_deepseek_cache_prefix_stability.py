"""DeepSeek's context cache is an exact-prefix match: a round whose request
carries the prior round's request byte-for-byte pays cache-hit prices (DeepSeek
Flash input is 0.0028 USD/M on a hit vs 0.14 USD/M on a miss — 50x) on every
already-emitted token. Rewriting any part of that prefix — moving an internal
``role=user`` boundary forward, or stripping ``reasoning_content`` from a batch
that was sent with it on an earlier round — turns the whole suffix into a miss.

The regression this guards: a transient "completed-step boundary" inserted at
the active chain's start made every consecutive round diverge from the prior
request at the previous round's boundary, and stripping reasoning from the
previously-active batch rewrote an already-sent message. Measured production
cache hits dropped from ~96% to ~56-69% with a proportionally larger bill.

What is asserted here, against the real outbound requests of the real
:class:`ConversationManager` (conventional agent loop, DeepSeek High thinking):

* round N+1 carries round N's request as an exact byte prefix and appends only
  the new suffix (tool result + continuation);
* previously emitted assistant reasoning is never rewritten;
* the active chain stays provider-valid — every assistant tool-call batch
  carries its ``reasoning_content``;
* the system prompt and the tool schemas are byte-identical on every round;
* canonical history is never mutated by request shaping.

The existing conventional-agent-loop regression suite
(``tests/test_production_agent_loop.py``) continues to drive the same loop and
is unaffected by this shaping policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.client import (
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    SELECTED_THINKING,
    Recorder,
    ScriptedBackend,
    build_manager,
    run,
)

#: How much thinking each scripted assistant round emits. Deliberately not tiny:
#: a real DeepSeek High turn spends thousands of reasoning tokens per batch, and
#: the cache regression is proportional to the reasoning being rewritten.
REASONING_STEP = (
    "Fix the retry cap so the job pauses. First read the module that owns the "
    "cap, then confirm where the loop counts attempts, then edit it to pause on "
    "the physical retry cap, then validate the file compiles."
)


def reasoning_tool_round(
    call_id: str, name: str, args: dict, *, reasoning: str = REASONING_STEP
) -> list:
    """One streamed round that ends in tool calls and carries thinking."""
    events: list = [ReasoningDelta(reasoning)]
    arguments = json.dumps(args)
    events.append(ToolCallStart(index=0, id=call_id, name=name))
    events.append(ToolCallArgsDelta(index=0, args_chunk=arguments))
    events.append(ToolCallEnd(index=0))
    events.append(Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant",
            "content": "",
            "reasoning_content": reasoning,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        },
    ))
    return events


def chat_stream(messages: list[dict]) -> bytes:
    """The chat-format token stream: one message per line, exactly as the
    provider's tokenizer sees it. A later request whose stream extends an
    earlier one byte-for-byte is a cache hit on the earlier request."""
    return b"".join(
        json.dumps(m, ensure_ascii=False).encode("utf-8") + b"\n"
        for m in messages
    )


def _recorded_rounds(backend: ScriptedBackend) -> list[dict]:
    """The outbound requests exactly as the provider received them."""
    return [dict(call) for call in backend.calls]


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


# ── 1: consecutive DeepSeek High production rounds keep a stable prefix ─────


class TestRequestPrefixIsStableAcrossRounds:
    """Drive the real loop over a real workspace with a scripted backend whose
    rounds carry thinking, then compare the recorded outbound requests."""

    def test_each_round_extends_the_prior_request_exactly(
        self, tmp_path: Path, isolated_streams,
    ) -> None:
        workspace = tmp_path / "proj"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "retry.py").write_text(
            "RETRY_CAP = 10\n", encoding="utf-8"
        )
        (workspace / "loop.py").write_text(
            "attempts = 0\n", encoding="utf-8"
        )
        backend = ScriptedBackend([
            reasoning_tool_round("r0", "read_file", {"path": "retry.py"}),
            reasoning_tool_round("r1", "read_file", {"path": "loop.py"}),
            reasoning_tool_round("w0", "write_file", {
                "path": "retry.py",
                "content": "RETRY_CAP = 10\nPAUSE_AFTER = 10\n",
            }),
            [
                ContentDelta(text="Updated retry.py; the loop pauses on the cap."),
                Done(finish_reason="stop", full_message={
                    "role": "assistant",
                    "content": "Updated retry.py; the loop pauses on the cap.",
                }),
            ],
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(
            workspace, "Fix the retry cap so the job pauses."
        )
        recorder = Recorder()

        run(manager, recorder, thinking=SELECTED_THINKING)

        calls = _recorded_rounds(backend)
        assert len(calls) == 4
        streams = [chat_stream(call["messages"]) for call in calls]

        # DeepSeek High on every active request, one stable catalog.
        assert backend.every_request_thinking() == SELECTED_THINKING
        hashes = {backend.schema_hash(i) for i in range(len(calls))}
        assert len(hashes) == 1, "the tool schema hash moved between requests"

        # Round N+1 carries round N's stream as an exact byte prefix.
        for i in range(1, len(streams)):
            assert streams[i].startswith(streams[i - 1]), (
                f"request {i} rewrote an already-sent prefix of request {i - 1}; "
                "the DeepSeek cache is a miss from that point onward"
            )
            assert len(streams[i]) > len(streams[i - 1]), (
                f"request {i} must only append the new suffix"
            )

    def test_reasoning_is_replayed_verbatim_on_every_round(
        self, tmp_path: Path, isolated_streams,
    ) -> None:
        workspace = tmp_path / "proj"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "retry.py").write_text(
            "RETRY_CAP = 10\n", encoding="utf-8"
        )
        backend = ScriptedBackend([
            reasoning_tool_round("r0", "read_file", {"path": "retry.py"}),
            reasoning_tool_round("w0", "write_file", {
                "path": "retry.py",
                "content": "RETRY_CAP = 10\nPAUSE_AFTER = 10\n",
            }),
            [
                ContentDelta(text="Done."),
                Done(finish_reason="stop", full_message={
                    "role": "assistant", "content": "Done.",
                }),
            ],
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(
            workspace, "Fix the retry cap so the job pauses."
        )
        recorder = Recorder()

        run(manager, recorder, thinking=SELECTED_THINKING)

        # Every assistant tool-call message the provider saw keeps its
        # reasoning — the replay rule is never violated and, because it is
        # never rewritten, the prefix stays exact.
        seen = 0
        for call in _recorded_rounds(backend):
            for msg in call["messages"]:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    assert msg.get("reasoning_content") == REASONING_STEP, (
                        "an assistant tool-call batch lost or altered its reasoning"
                    )
                    seen += 1
        assert seen == 3, f"expected 3 scripted tool batches, saw {seen}"

    def test_system_prompt_and_tool_schema_are_byte_stable(
        self, tmp_path: Path, isolated_streams,
    ) -> None:
        workspace = tmp_path / "proj"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "retry.py").write_text(
            "RETRY_CAP = 10\n", encoding="utf-8"
        )
        backend = ScriptedBackend([
            reasoning_tool_round("r0", "read_file", {"path": "retry.py"}),
            reasoning_tool_round("w0", "write_file", {
                "path": "retry.py",
                "content": "RETRY_CAP = 10\nPAUSE_AFTER = 10\n",
            }),
            [
                ContentDelta(text="Done."),
                Done(finish_reason="stop", full_message={
                    "role": "assistant", "content": "Done.",
                }),
            ],
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(
            workspace, "Fix the retry cap so the job pauses."
        )
        recorder = Recorder()

        run(manager, recorder, thinking=SELECTED_THINKING)

        calls = _recorded_rounds(backend)
        systems = [
            json.dumps([m for m in call["messages"] if m.get("role") == "system"],
                       ensure_ascii=False)
            for call in calls
        ]
        assert len(set(systems)) == 1, "the system prompt moved between requests"
        schemas = [
            json.dumps(call["tools"], sort_keys=True, ensure_ascii=False)
            for call in calls
        ]
        assert len(set(schemas)) == 1, "the tool schema moved between requests"


# ── 2: deterministic api-view-level serialisation ───────────────────────────


class TestDeterministicViewPrefix:
    """The same stability, proven directly on ``build_api_view`` output with no
    manager in the way — a fast, fully deterministic reproduction."""

    def _history(self, rounds: int) -> "object":
        from aura.conversation.history import History

        h = History()
        h.set_system("You are Aura's production coding agent.")
        h.append_user_text("Fix the retry cap so the job pauses.")
        for i in range(rounds):
            assistant = {
                "role": "assistant",
                "content": "",
                "reasoning_content": f"thinking about step {i}\n" + ("r" * (i * 500)),
                "tool_calls": [{
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "read_files", "arguments": json.dumps({
                        "paths": [f"f{i}.py"],
                    })},
                }],
            }
            h.messages.append(assistant)
            h.messages.append({
                "role": "tool",
                "tool_call_id": f"c{i}",
                "content": json.dumps({"ok": True, "files": {
                    f"f{i}.py": {"ok": True, "status": "complete",
                                 "content": "# f\n" + ("x" * 4_000)},
                }}),
            })
        return h

    def test_consecutive_views_extend_the_prior_request_byte_for_byte(self) -> None:
        from aura.conversation.api_view import build_api_view

        payloads: list[bytes] = []
        for n in range(1, 6):
            h = self._history(n)
            view = build_api_view(h.system_prompt, h.messages, 10_000_000)
            payloads.append(chat_stream(view.messages))
            assert view.stats.reasoning_chars_dropped == 0, (
                f"round {n} shed intra-turn reasoning"
            )
            assert all(
                not (m.get("role") == "user" and m.get("aura_internal"))
                for m in view.messages
            )
        for i in range(1, len(payloads)):
            assert payloads[i].startswith(payloads[i - 1]), (
                f"view {i} broke the byte prefix of view {i - 1}"
            )

    def test_canonical_history_is_not_mutated_by_request_shaping(self) -> None:
        from aura.conversation.api_view import build_api_view

        h = self._history(4)
        before = json.dumps(h.messages, sort_keys=True)

        build_api_view(h.system_prompt, h.messages, 10_000_000)
        build_api_view(h.system_prompt, h.messages, 1_000)

        assert json.dumps(h.messages, sort_keys=True) == before
