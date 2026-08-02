"""The context budget, the non-destructive API view, and structured reads.

These cover the failure that made long coding turns burn tokens without making
progress: history was pruned in place against one hardcoded 60K cap, and tool
results — which are JSON — were cut with a raw character prefix
(``111537 -> 8000``), so the model received shredded, unparseable evidence for
the very file it was working on and read it again.

What is asserted here:

* DeepSeek's reasoning replay rule survives compaction;
* ``for_api()`` never edits the stored log;
* the *active model's* budget actually reaches the compaction pass;
* a compacted tool result is still valid JSON;
* every ``read_files`` path keeps its metadata, before and after compaction;
* completed tool blocks are dropped whole, never orphaning a tool message;
* unknown models fall back to a conservative budget;
* the large-current-turn case no longer degenerates into prefix loss.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from aura.client import ContentDelta, Done
from aura.config import (
    CONTEXT_WORKING_SET_FRACTION,
    FALLBACK_CONTEXT_WINDOW_TOKENS,
    MIN_WORKING_SET_TOKENS,
)
from aura.conversation.api_view import (
    build_api_view,
    compact_result_content,
    estimate_tokens,
)
from aura.conversation.context_budget import resolve_model_budget
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.fs_handler import (
    READ_FILES_FULL_INCLUDE_CHARS,
    FsReadHandler,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

# ── helpers ─────────────────────────────────────────────────────────────────


def assert_tool_pairing_valid(messages: list[dict[str, Any]]) -> None:
    """Every tool message answers a tool_call in the assistant right above it."""
    i = 0
    seen_tool_ids: set[str] = set()
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool":
            raise AssertionError(
                f"tool message at {i} has no preceding assistant tool_calls block"
            )
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue

        expected = [tc["id"] for tc in msg["tool_calls"]]
        j = i + 1
        answered: list[str] = []
        while j < len(messages) and messages[j].get("role") == "tool":
            call_id = messages[j].get("tool_call_id")
            assert call_id not in seen_tool_ids, f"duplicate tool result {call_id}"
            seen_tool_ids.add(call_id)
            answered.append(call_id)
            j += 1
        assert answered == expected, (
            f"assistant at {i} expected results {expected}, got {answered}"
        )
        i = j


def tool_block(
    call_id: str,
    name: str,
    args: dict[str, Any],
    result: Any,
    *,
    reasoning: str = "",
) -> list[dict[str, Any]]:
    """An assistant tool-call message plus its tool result."""
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }
    if reasoning:
        assistant["reasoning_content"] = reasoning
    payload = result if isinstance(result, str) else json.dumps(result)
    return [assistant, {"role": "tool", "tool_call_id": call_id, "content": payload}]


def big_read_files_result(paths: list[str], chars_each: int) -> str:
    """A realistic read_files payload of a known size."""
    files = {
        p: {
            "ok": True,
            "path": p,
            "status": "complete",
            "reason": "file included in full",
            "file_size": chars_each,
            "content_hash": f"hash-{i}",
            "line_count": 3,
            "included_range": {"start_line": 1, "end_line": 3},
            "content": f"# {p}\n" + ("x" * chars_each) + "\n",
            "continuation": "",
        }
        for i, p in enumerate(paths)
    }
    return json.dumps({"ok": True, "files": files, "requested_paths": paths})


# ── 1: DeepSeek reasoning replay ────────────────────────────────────────────


class TestReasoningReplay:
    """THE BOUNDARY: reasoning must survive on the active tool-call chain.

    DeepSeek's thinking-mode replay rule binds the assistant messages *after*
    the last user message — the chain the provider is about to continue. Before
    that boundary reasoning is dead weight and may be dropped; the strip runs
    before compaction so the space it frees comes out of reasoning, not out of
    tool results.
    """

    def _history(self) -> History:
        h = History()
        h.set_system("system prompt")
        for turn in range(6):
            h.append_user_text(f"turn {turn} request")
            for msg in tool_block(
                f"c{turn}", "read_files",
                {"paths": [f"f{turn}.py"]},
                big_read_files_result([f"f{turn}.py"], 4_000),
                reasoning=f"thinking about turn {turn}\n",
            ):
                h.messages.append(msg)
            h.append_assistant({
                "role": "assistant",
                "content": f"answer {turn}",
                "reasoning_content": f"summarising turn {turn}\n",
            })
        return h

    @staticmethod
    def _last_user_index(messages: list[dict[str, Any]]) -> int:
        users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        assert users, "view must contain the user's request"
        return users[-1]

    def test_reasoning_survives_an_uncompacted_view(self) -> None:
        h = self._history()
        input_messages = h.messages
        view = build_api_view("system prompt", input_messages, 10_000_000)
        boundary = self._last_user_index(view.messages)

        # Every assistant message after the last user message keeps its
        # reasoning — this is the one that prevents the 400.
        after = [m for m in view.messages[boundary + 1:] if m.get("role") == "assistant"]
        assert len(after) == 2
        assert all(m.get("reasoning_content") for m in after)
        # Finished turns before the boundary shed it entirely.
        before = [m for m in view.messages[:boundary] if m.get("role") == "assistant"]
        assert len(before) == 10
        assert all("reasoning_content" not in m for m in before)
        # The counter accounts for exactly what was removed: five finished
        # turns × two reasoning blocks each (22 + 19 chars).
        assert view.stats.reasoning_chars_dropped == 5 * (22 + 19)
        assert view.stats.reasoning_chars_replayed == 22 + 19
        # Canonical history is never mutated by the strip.
        assert sum(
            len(m.get("reasoning_content") or "")
            for m in input_messages if m.get("role") == "assistant"
        ) == 6 * (22 + 19)

    @pytest.mark.parametrize("budget", [40_000, 8_000, 2_000, 400])
    def test_reasoning_survives_aggressive_compaction(self, budget) -> None:
        h = self._history()
        view = build_api_view(h.system_prompt, h.messages, budget)

        # The strip runs before the ladder, so the dropped-reasoning counter
        # is identical at every budget, and whatever active-chain assistant
        # survives compaction still carries its reasoning.
        assert view.stats.reasoning_chars_dropped == 5 * (22 + 19)
        boundary = self._last_user_index(view.messages)
        active_chain = [
            m for m in view.messages[boundary + 1:]
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        for msg in active_chain:
            assert msg.get("reasoning_content"), (
                "a retained active-chain assistant lost reasoning_content — "
                "DeepSeek rejects this replay with a 400"
            )
        assert_tool_pairing_valid(view.messages)

    def test_reasoning_is_kept_on_assistants_without_tool_calls(self) -> None:
        h = History()
        h.set_system("s")
        h.append_user_text("hi")
        h.append_assistant({
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "deliberating\n",
        })
        api = h.for_api()
        assistant = next(m for m in api if m["role"] == "assistant")
        assert assistant["reasoning_content"] == "deliberating\n"

    def test_no_user_message_means_nothing_is_stripped(self) -> None:
        """Conservative path: without a user message there is no boundary, so
        every block keeps its reasoning."""
        h = History()
        h.set_system("s")
        for msg in tool_block(
            "c0", "read_files", {"paths": ["f0.py"]},
            big_read_files_result(["f0.py"], 4_000),
            reasoning="thinking\n",
        ):
            h.messages.append(msg)

        view = build_api_view("s", h.messages, 10_000_000)
        assert view.stats.reasoning_chars_dropped == 0
        replayed = [m for m in view.messages if m.get("reasoning_content")]
        assert len(replayed) == 1

    def test_internal_steering_messages_are_the_provider_boundary(self) -> None:
        """Aura's steering messages go out as role=user, so the provider sees
        them as the boundary; reasoning before them is stripped."""
        h = History()
        h.set_system("s")
        h.append_user_text("Fix the retry cap.")
        for msg in tool_block(
            "c1", "read_file", {"path": "m.py"},
            json.dumps({"ok": True, "content": "x" * 200}),
            reasoning="thinking about the cap\n",
        ):
            h.messages.append(msg)
        h.append_internal_user_text("Loop guard: 12 discovery calls have run.")
        h.append_assistant({
            "role": "assistant",
            "content": "on it",
            "reasoning_content": "still thinking\n",
        })

        view = build_api_view("s", h.messages, 10_000_000)
        boundary = self._last_user_index(view.messages)
        assert view.messages[boundary]["content"].startswith("Loop guard:")
        assert view.stats.reasoning_chars_dropped == len("thinking about the cap\n")
        after = [m for m in view.messages[boundary + 1:] if m.get("role") == "assistant"]
        assert all(m.get("reasoning_content") for m in after)

    def test_strip_runs_before_compaction_not_after(self, monkeypatch) -> None:
        """Reasoning is shed so evidence is not: with the strip, an overflowing
        history fits with zero dropped blocks; without it, the ladder has to
        drop whole blocks of tool results to make room."""
        from aura.conversation import api_view as api_view_module

        h = History()
        h.set_system("system prompt")
        for turn in range(6):
            h.append_user_text(f"turn {turn} request")
            for msg in tool_block(
                f"c{turn}", "read_files",
                {"paths": [f"f{turn}.py"]},
                big_read_files_result([f"f{turn}.py"], 4_000),
                reasoning=f"{turn}" * 30_000,
            ):
                h.messages.append(msg)
            h.append_assistant({
                "role": "assistant",
                "content": f"answer {turn}",
                "reasoning_content": f"summarising turn {turn}\n",
            })

        view_with = build_api_view(h.system_prompt, h.messages, 40_000)
        assert view_with.stats.reasoning_chars_dropped > 0
        assert view_with.stats.dropped_blocks == 0

        monkeypatch.setattr(
            api_view_module, "_strip_superseded_reasoning", lambda working, stats: None
        )
        view_without = build_api_view(h.system_prompt, h.messages, 40_000)
        assert view_without.stats.reasoning_chars_dropped == 0
        assert view_without.stats.dropped_blocks > 0


# ── 2: for_api() does not mutate stored history ─────────────────────────────


class TestForApiIsNonDestructive:

    def test_heavy_compaction_leaves_storage_exact(self) -> None:
        h = History()
        h.set_system("system")
        for turn in range(5):
            h.append_user_text(f"request {turn}")
            for msg in tool_block(
                f"c{turn}", "read_files", {"paths": [f"m{turn}.py"]},
                big_read_files_result([f"m{turn}.py"], 20_000),
                reasoning="thinking\n",
            ):
                h.messages.append(msg)

        before = json.dumps(h.messages, sort_keys=True)
        tokens_before = h.estimate_tokens()

        view = h.build_api_payload(500)

        assert json.dumps(h.messages, sort_keys=True) == before, (
            "for_api()/build_api_payload() edited the canonical log"
        )
        assert h.estimate_tokens() == tokens_before
        assert view.stats.tokens_after < view.stats.tokens_before

    def test_repair_of_orphans_happens_on_the_copy_only(self) -> None:
        h = History()
        h.set_system("system")
        h.append_user_text("go")
        # An interrupted turn: a tool_calls assistant whose result never arrived.
        h.append_assistant({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "orphan",
                "type": "function",
                "function": {"name": "read_files", "arguments": "{}"},
            }],
        })
        stored_len = len(h.messages)

        api = h.for_api()

        assert len(h.messages) == stored_len, "storage was repaired behind the user's back"
        assert not any(m.get("tool_calls") for m in api), (
            "the unreplayable block must not be sent"
        )
        assert_tool_pairing_valid(api)

    def test_repeated_calls_are_stable(self) -> None:
        h = History()
        h.set_system("system")
        h.append_user_text("go")
        for msg in tool_block("c1", "read_files", {"paths": ["a.py"]},
                              big_read_files_result(["a.py"], 30_000)):
            h.messages.append(msg)

        first = h.build_api_payload(400).messages
        second = h.build_api_payload(400).messages
        assert first == second, "compaction is not idempotent across calls"


# ── 3: the active model's budget reaches history pruning ────────────────────


class TestBudgetReachesPruning:

    def _run_capturing_budget(
        self, tmp_path: Path, monkeypatch, model: str
    ) -> list[int | None]:
        import aura.conversation.manager as manager_module

        # A fresh registry per run — handlers cannot be re-registered.
        registry = ModelStreamRegistry()
        monkeypatch.setattr(manager_module, "model_streams", registry)

        seen: list[int | None] = []

        h = History()
        h.set_system("You are Aura's production coding agent.")
        h.append_user_text("do the thing")
        real_build = h.build_api_payload

        def spy(max_tokens: int | None = None):
            seen.append(max_tokens)
            return real_build(max_tokens)

        h.build_api_payload = spy  # type: ignore[method-assign]

        def stream(**kwargs):
            return iter([
                ContentDelta(text="done"),
                Done(finish_reason="stop",
                     full_message={"role": "assistant", "content": "done"}),
            ])

        registry.register(PRODUCTION_STREAM_HOOK, stream)
        workspace = tmp_path / f"ws-{model or 'none'}".replace("/", "-")
        workspace.mkdir()
        manager = ConversationManager(h, ToolRegistry(workspace_root=workspace, mode="single"))
        manager.send(
            on_event=lambda ev: None,
            approval_cb=lambda req: None,
            cancel_event=threading.Event(),
            model=model,
            thinking="off",
            hook_name=PRODUCTION_STREAM_HOOK,
            max_tool_rounds=2,
        )
        return seen

    def test_catalog_model_budget_is_used(self, tmp_path, monkeypatch) -> None:
        seen = self._run_capturing_budget(tmp_path, monkeypatch, "deepseek-v4-flash")
        expected = resolve_model_budget("deepseek-v4-flash").working_set_tokens
        assert seen and seen[0] == expected
        assert expected != 60_000, "the send path is still on the hardcoded cap"

    def test_a_different_model_gets_a_different_budget(
        self, tmp_path, monkeypatch
    ) -> None:
        big = self._run_capturing_budget(tmp_path, monkeypatch, "gemini-2.5-pro")
        small = self._run_capturing_budget(tmp_path, monkeypatch, "deepseek-v4-flash")
        assert big[0] == resolve_model_budget("gemini-2.5-pro").working_set_tokens
        assert big[0] > small[0], "budget does not track the active model"

    def test_budget_is_actually_applied_by_the_view(self) -> None:
        h = History()
        h.set_system("s")
        h.append_user_text("go")
        for msg in tool_block("c1", "read_files", {"paths": ["a.py"]},
                              big_read_files_result(["a.py"], 60_000)):
            h.messages.append(msg)

        generous = h.build_api_payload(1_000_000)
        tight = h.build_api_payload(2_000)
        assert generous.stats.tokens_after > tight.stats.tokens_after
        assert generous.stats.compacted_results == 0
        assert tight.stats.compacted_results >= 1


# ── 4: compacted results stay valid JSON ────────────────────────────────────


class TestCompactedResultsRemainValidJson:

    def test_structure_survives_a_hard_squeeze(self) -> None:
        original = big_read_files_result(["a.py", "b.py", "c.py"], 40_000)
        compacted, changed = compact_result_content(original, 3_000, "read_files")

        assert changed
        assert len(compacted) < len(original)
        parsed = json.loads(compacted)  # must not raise
        assert parsed["ok"] is True
        assert set(parsed["files"]) == {"a.py", "b.py", "c.py"}

    def test_below_the_minimum_leaf_it_still_parses(self) -> None:
        original = big_read_files_result([f"f{i}.py" for i in range(40)], 5_000)
        # Far below what 40 envelopes can possibly serialise into.
        compacted, changed = compact_result_content(original, 200, "read_files")

        assert changed
        parsed = json.loads(compacted)
        assert len(parsed["files"]) == 40, (
            "the envelope must survive even when the byte budget cannot"
        )

    def test_non_json_results_still_get_a_marked_cut(self) -> None:
        compacted, changed = compact_result_content("y" * 50_000, 1_000, "run_terminal_command")
        assert changed
        assert "truncated" in compacted
        with pytest.raises(json.JSONDecodeError):
            json.loads(compacted)  # it was never JSON to begin with

    def test_every_tool_result_in_a_compacted_view_still_parses(self) -> None:
        h = History()
        h.set_system("s")
        for turn in range(4):
            h.append_user_text(f"turn {turn}")
            for msg in tool_block(
                f"c{turn}", "read_files", {"paths": [f"f{turn}.py"]},
                big_read_files_result([f"f{turn}.py", f"g{turn}.py"], 25_000),
                reasoning="thinking\n",
            ):
                h.messages.append(msg)

        view = h.build_api_payload(1_500)
        tool_msgs = [m for m in view.messages if m.get("role") == "tool"]
        assert tool_msgs
        for msg in tool_msgs:
            json.loads(msg["content"])  # must not raise


# ── 5: every read_files path stays represented ──────────────────────────────


@pytest.fixture
def read_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "small.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "medium.py").write_text("# medium\n" + "z = 1\n" * 200, encoding="utf-8")
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(6_000))
    (root / "big.py").write_text(big, encoding="utf-8")
    (root / "huge.py").write_text(big * 3, encoding="utf-8")
    return root


def make_handler(root: Path) -> FsReadHandler:
    def resolve(raw: str) -> Path:
        candidate = (root / raw).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            raise ValueError(f"path escapes workspace: {raw}")
        return candidate

    return FsReadHandler(root, resolve)


class TestReadFilesKeepsEveryPath:

    def test_small_files_are_complete_and_lossless(self, read_workspace) -> None:
        payload = make_handler(read_workspace).handle_read_files({"paths": ["small.py"]})
        entry = payload["files"]["small.py"]

        assert entry["status"] == "complete"
        assert entry["truncated"] is False
        # Compare against raw bytes — read_file decodes without newline translation.
        assert entry["content"] == (read_workspace / "small.py").read_bytes().decode("utf-8")
        assert entry["included_range"] == {"start_line": 1, "end_line": 2}
        assert entry["content_hash"] and entry["file_size"] > 0

    def test_large_files_get_bounded_content_and_a_route_to_the_rest(
        self, read_workspace
    ) -> None:
        payload = make_handler(read_workspace).handle_read_files({"paths": ["huge.py"]})
        entry = payload["files"]["huge.py"]

        assert entry["status"] in {"summarized", "truncated"}
        assert entry["truncated"] is True
        assert 0 < len(entry["content"]) < READ_FILES_FULL_INCLUDE_CHARS
        assert entry["included_range"]["start_line"] == 1
        assert "read_file" in entry["continuation"]
        assert "offset" in entry["continuation"]
        assert entry["reason"]

    def test_missing_and_escaping_paths_still_get_entries(self, read_workspace) -> None:
        payload = make_handler(read_workspace).handle_read_files(
            {"paths": ["small.py", "nope.py", "../outside.py"]}
        )
        files = payload["files"]

        assert set(files) == {"small.py", "nope.py", "../outside.py"}
        assert files["nope.py"]["status"] == "error"
        assert files["nope.py"]["reason"]
        assert files["../outside.py"]["status"] == "error"

    def test_budget_exhaustion_omits_content_not_metadata(self, read_workspace) -> None:
        paths = ["huge.py"] * 1  # the shared budget is large; force it instead
        handler = make_handler(read_workspace)
        payload = handler.handle_read_files({"paths": paths})
        assert payload["files"]["huge.py"]["file_size"] > 0

        # Directly exercise the exhausted-budget branch.
        entry, used = handler._read_one_for_batch("small.py", remaining=0)
        assert used == 0
        assert entry["status"] == "omitted"
        assert entry["content"] == ""
        assert entry["file_size"] > 0
        assert entry["content_hash"]
        assert "read_file" in entry["continuation"]
        assert "offset" in entry["continuation"]

    def test_every_path_survives_history_compaction(self, read_workspace) -> None:
        paths = ["small.py", "medium.py", "big.py", "huge.py", "nope.py"]
        payload = make_handler(read_workspace).handle_read_files({"paths": paths})
        result = json.dumps(payload)

        h = History()
        h.set_system("s")
        h.append_user_text("read them")
        for msg in tool_block("c1", "read_files", {"paths": paths}, result):
            h.messages.append(msg)

        view = h.build_api_payload(600)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")
        parsed = json.loads(tool_msg["content"])

        assert set(parsed["files"]) == set(paths), (
            "compaction dropped a requested path — the model cannot tell what it "
            "still has to read"
        )
        for path in paths:
            entry = parsed["files"][path]
            assert entry.get("status"), f"{path} lost its status"
            assert entry.get("reason"), f"{path} lost its reason"


# ── 6: completed blocks compact without orphaning tool messages ─────────────


class TestBlockCompaction:

    def _many_blocks(self) -> History:
        h = History()
        h.set_system("system")
        for turn in range(8):
            h.append_user_text(f"turn {turn}")
            for msg in tool_block(
                f"a{turn}", "read_files", {"paths": [f"x{turn}.py"]},
                big_read_files_result([f"x{turn}.py"], 12_000),
                reasoning="thinking\n",
            ):
                h.messages.append(msg)
            for msg in tool_block(
                f"b{turn}", "grep_search", {"pattern": "def"},
                big_read_files_result([f"y{turn}.py"], 12_000),
            ):
                h.messages.append(msg)
        return h

    @pytest.mark.parametrize("budget", [50_000, 20_000, 5_000, 1_000, 300])
    def test_pairing_holds_at_every_budget(self, budget) -> None:
        h = self._many_blocks()
        view = h.build_api_payload(budget)
        assert_tool_pairing_valid(view.messages)

    def test_blocks_are_dropped_whole(self) -> None:
        h = self._many_blocks()
        view = h.build_api_payload(2_000)

        assert view.stats.dropped_blocks > 0, "no block was dropped under real pressure"
        assert_tool_pairing_valid(view.messages)
        # Nothing references a tool_call that is no longer present.
        present = {
            tc["id"]
            for m in view.messages
            for tc in (m.get("tool_calls") or [])
        }
        answered = {
            m["tool_call_id"] for m in view.messages if m.get("role") == "tool"
        }
        assert answered == present

    def test_dropping_prefers_old_material_over_the_current_turn(self) -> None:
        h = self._many_blocks()
        last_call_id = "b7"
        view = h.build_api_payload(30_000)

        answered = {m["tool_call_id"] for m in view.messages if m.get("role") == "tool"}
        assert last_call_id in answered, (
            "the current turn's evidence was sacrificed before older turns"
        )


# ── 7: unknown models fall back safely ──────────────────────────────────────


class TestUnknownModelFallback:

    @pytest.mark.parametrize(
        "model",
        ["", None, "not-a-real-model", "vendor/some-preview-2099", "openrouter/owl-alpha"],
    )
    def test_fallback_is_conservative_and_never_raises(self, model) -> None:
        budget = resolve_model_budget(model)

        assert budget.is_fallback is True
        assert budget.context_window_tokens == FALLBACK_CONTEXT_WINDOW_TOKENS
        assert budget.working_set_tokens >= MIN_WORKING_SET_TOKENS
        assert budget.working_set_tokens < budget.context_window_tokens, (
            "the fallback must not try to fill the whole assumed window"
        )

    def test_known_models_are_not_marked_fallback(self) -> None:
        budget = resolve_model_budget("deepseek-v4-flash")
        assert budget.is_fallback is False
        assert budget.context_window_tokens == 128_000
        expected = int(
            (budget.context_window_tokens - budget.output_reserve_tokens)
            * CONTEXT_WORKING_SET_FRACTION
        )
        assert budget.working_set_tokens == expected

    def test_output_room_is_always_reserved(self) -> None:
        for model in ["deepseek-v4-flash", "claude-sonnet-4-6", "gemini-2.5-pro", "mystery"]:
            budget = resolve_model_budget(model)
            headroom = budget.context_window_tokens - budget.working_set_tokens
            assert headroom >= budget.output_reserve_tokens, (
                f"{model} leaves no room for its own response"
            )


# ── 8: the observed large-current-turn regression ───────────────────────────


class TestLargeCurrentTurnNoLongerCollapses:
    """The reported case: a 111537-char current-turn read cut to an 8000-char prefix."""

    OBSERVED_CHARS = 111_537

    def _history_with_big_current_read(self) -> tuple[History, str]:
        paths = [f"src/module_{i}.py" for i in range(6)]
        result = big_read_files_result(paths, self.OBSERVED_CHARS // len(paths))
        h = History()
        h.set_system("You are Aura's production coding agent." * 40)
        h.append_user_text("Fix the retry cap so the job pauses.")
        for msg in tool_block(
            "read-1", "read_files", {"paths": paths}, result, reasoning="planning\n"
        ):
            h.messages.append(msg)
        return h, result

    def test_the_real_deepseek_budget_keeps_the_read_intact(self) -> None:
        h, result = self._history_with_big_current_read()
        budget = resolve_model_budget("deepseek-v4-flash")

        view = h.build_api_payload(budget.working_set_tokens)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")

        assert tool_msg["content"] == result, (
            "a single large current-turn read still fits this model's window and "
            "must not be touched at all"
        )
        assert view.stats.compacted_results == 0
        assert view.stats.source_result_chars_retained == view.stats.source_result_chars_generated

    def test_under_real_pressure_the_loss_is_structural_not_a_prefix(self) -> None:
        h, result = self._history_with_big_current_read()
        # A budget far below the payload, so compaction is unavoidable.
        view = h.build_api_payload(4_000)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")

        assert tool_msg["content"] != result
        parsed = json.loads(tool_msg["content"])  # the old prefix cut could not do this
        assert len(parsed["files"]) == 6, "every requested path is still accounted for"
        for entry in parsed["files"].values():
            assert entry["status"] == "complete"
            assert entry["content_hash"]
            assert entry["path"]

    def test_the_old_8000_char_floor_is_no_longer_the_outcome(self) -> None:
        h, _ = self._history_with_big_current_read()
        budget = resolve_model_budget("deepseek-v4-flash")
        view = h.build_api_payload(budget.working_set_tokens)

        retained = view.stats.source_result_chars_retained
        assert retained > 8_000 * 4, (
            f"current-turn source evidence collapsed to {retained} chars; the "
            f"budget should have kept all {self.OBSERVED_CHARS}"
        )

    def test_estimates_are_consistent_with_the_budget_decision(self) -> None:
        h, _ = self._history_with_big_current_read()
        budget = resolve_model_budget("deepseek-v4-flash")
        view = h.build_api_payload(budget.working_set_tokens)

        assert view.stats.tokens_before == estimate_tokens(h.messages, h.system_prompt)
        assert view.stats.over_budget is False
        assert view.stats.budget_tokens == budget.working_set_tokens
        assert view.stats.system_prompt_chars == len(h.system_prompt)
