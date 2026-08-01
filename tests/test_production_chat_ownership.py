"""Ownership contract for production turns: chat owns prose, workspace owns work.

Every normal send runs the production execution path, whatever lane
``classify_user_request`` assigns it.  That path must still honour one
ownership split:

* Chat owns the user and assistant conversation prose, and persists it.
* Workspace / Worker surfaces own execution activity, tools, diffs, terminal
  output, validation evidence, and a distinct completion receipt.

No assistant answer may exist only in the ephemeral Worker Log, and final prose
must never be duplicated into it.

These tests drive the real ConversationBridge, the real Qt signal wiring, and
real save/load, offscreen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from aura.client import (  # noqa: E402
    ApiError,
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.conversation.chat_transcript import ERROR, PLANNER, USER  # noqa: E402
from aura.conversation.persistence import load_conversation, save_conversation  # noqa: E402
from aura.model_streams import (  # noqa: E402
    PLANNER_STREAM_HOOK,
    PRODUCTION_STREAM_HOOK,
    WORKER_STREAM_HOOK,
    model_streams,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("Toy project.\n", encoding="utf-8")
    return root


@pytest.fixture
def restore_streams():
    saved = {
        name: model_streams.get_handler(name)
        for name in (PRODUCTION_STREAM_HOOK, PLANNER_STREAM_HOOK, WORKER_STREAM_HOOK)
    }
    yield
    for name, handler in saved.items():
        model_streams.unregister(name)
        if handler is not None:
            model_streams.register(name, handler)


# ── scripted rounds ─────────────────────────────────────────────────────────


def _final_round(text: str) -> list:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


def _tool_round(calls: list[tuple[str, str, dict]], text: str = "") -> list:
    events: list = []
    if text:
        events.append(ContentDelta(text=text))
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={"role": "assistant", "content": text, "tool_calls": tool_calls},
    ))
    return events


class ScriptedBackend:
    def __init__(self, rounds: list[list]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(_final_round("(script exhausted)"))


def install_production_backend(handler) -> None:
    """Install after bridge construction — the bridge registers its own hook."""
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, handler)


# ── harness: real bridge + real GUI wiring, MainWindow's lifecycle ──────────


class ChatOwnershipHarness:
    """Real bridge, ChatView, AuraPlayground, WorkerEventHandler + persistence.

    Reproduces MainWindow's send / stream-done / finished lifecycle, including
    the queue drain and the auto-save point, without booting the whole window.
    """

    def __init__(self, workspace: Path) -> None:
        from aura.bridge import ConversationBridge
        from aura.config import AppSettings
        from aura.gui.chat_view import ChatView
        from aura.gui.playground import AuraPlayground
        from aura.gui.worker_handler import WorkerEventHandler

        self.workspace = workspace
        self.parent = QWidget()
        self.settings = AppSettings.from_dict({})
        self.bridge = ConversationBridge(
            parent_widget=self.parent, provider=self.settings.provider
        )
        self.bridge.set_production_provider(self.settings.provider)
        self.bridge.set_workspace_root(workspace)
        self.bridge.set_production_mode()
        self.bridge.set_auto_approve(True)

        self.chat = ChatView()
        self.chat.setParent(self.parent)
        self.chat.set_compact_tools(True)
        self.playground = AuraPlayground(parent=self.parent)
        self.playground.set_workspace_root(workspace)

        self.worker_handler = WorkerEventHandler(
            bridge=self.bridge,
            chat=self.chat,
            playground=self.playground,
            settings=self.settings,
            parent=self.parent,
        )
        self.worker_handler.connect_bridge_signals()

        self.chat_content: list[str] = []
        self.saves: list[list[dict]] = []
        self.queue: list[str] = []
        self.saved_path: Path | None = None

        # MainWindow's bridge ↔ view wiring (group 12).
        self.bridge.contentDelta.connect(self.chat.append_content)
        self.bridge.contentDelta.connect(self.chat_content.append)
        self.bridge.reasoningDelta.connect(self.chat.append_reasoning)
        self.bridge.toolCallStart.connect(self.chat.add_tool_call)
        self.bridge.toolCallArgs.connect(self.chat.append_tool_args)
        self.bridge.toolResult.connect(
            lambda tid, _n, ok, result, _e: self.chat.set_tool_result(tid, ok, result)
        )
        self.bridge.streamDone.connect(self._on_stream_done)
        self.bridge.apiError.connect(self._on_api_error)
        self.bridge.finished.connect(self._on_finished)

    # ---- MainWindow lifecycle mirrors ----

    def _on_stream_done(self, _finish_reason: str, full_message: dict) -> None:
        if full_message.get("tool_calls"):
            self.chat.finalize_markdown_only()
        else:
            self.chat.assistant_done()

    def _on_api_error(self, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Error"
        self.chat.add_error(title, message, show_retry=True)
        self.chat.stop_current_aura()

    def _on_finished(self) -> None:
        self.chat.assistant_done()
        self.chat.stop_current_aura()
        QTimer.singleShot(0, self.auto_save)
        QTimer.singleShot(0, self._drain_queue)

    def auto_save(self) -> None:
        """Synchronous stand-in for ConversationPersistence.auto_save."""
        items = list(self.chat.chat_items)
        self.saves.append(items)
        if not self.bridge.history.messages:
            return
        self.saved_path = save_conversation(
            history=self.bridge.history,
            workspace_root=self.workspace,
            model="scripted-production-model",
            thinking="off",
            existing_path=self.saved_path,
            chat_items=items,
        )

    def _drain_queue(self) -> None:
        if self.bridge.is_running() or not self.queue:
            return
        self.send(self.queue.pop(0))

    # ---- driving ----

    def send(self, user_text: str) -> None:
        self.bridge.history.append_user_text(user_text)
        self.chat.add_user(user_text)
        self.chat.begin_assistant()
        self.bridge.send(model="scripted-production-model", thinking="off")

    def run_turn(self, user_text: str, timeout_ms: int = 60000) -> bool:
        """Send one turn and pump Qt until it and its deferred work complete."""
        self.bridge.history.set_system("You are Aura's production coding agent.")
        loop = QEventLoop()
        completed = {"value": False}

        def _done() -> None:
            completed["value"] = True
            loop.quit()

        self.bridge.finished.connect(_done)
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(timeout_ms)

        self.send(user_text)
        loop.exec()
        guard.stop()
        self.bridge.finished.disconnect(_done)
        self.settle()
        return completed["value"]

    def settle(self, rounds: int = 60) -> None:
        for _ in range(rounds):
            QApplication.processEvents()

    def worker_log_text(self) -> str:
        return self.playground._info_hub._log_view.toPlainText()

    def assistant_texts(self) -> list[str]:
        return [i["text"] for i in self.chat.chat_items if i["kind"] == PLANNER]

    def user_texts(self) -> list[str]:
        return [i["text"] for i in self.chat.chat_items if i["kind"] == USER]

    def reload(self):
        """Load the saved conversation back through the real persistence path."""
        assert self.saved_path is not None, "nothing was saved"
        return load_conversation(self.saved_path)

    def teardown(self) -> None:
        try:
            self.playground.terminal_window().close()
        except Exception:
            pass
        self.parent.deleteLater()
        QApplication.processEvents()


@pytest.fixture
def harness(qapp, workspace, restore_streams):
    h = ChatOwnershipHarness(workspace)
    try:
        yield h
    finally:
        h.teardown()


def _render_into(chat, items: list[dict]) -> None:
    """Render persisted chat items exactly as ConversationPersistence does."""
    from aura.gui.conv_persistence import ConversationPersistence

    cp = ConversationPersistence.__new__(ConversationPersistence)
    cp._chat = chat
    cp._active_replay_id = 0
    cp._render_chat_items(items)


# ── 1–3: the conversational turn ────────────────────────────────────────────


ANSWER = "It is a tidy Qt app with a clean bridge boundary."


class TestConversationalTurn:
    def test_answer_reaches_chat_exactly_once(self, harness) -> None:
        backend = ScriptedBackend([_final_round(ANSWER)])
        install_production_backend(backend.stream)

        assert harness.run_turn("What do you think of this project?")

        assert harness.user_texts() == ["What do you think of this project?"]
        assert harness.assistant_texts() == [ANSWER]
        assert "".join(harness.chat_content).count(ANSWER) == 1

    def test_answer_is_not_duplicated_into_the_worker_log(self, harness) -> None:
        backend = ScriptedBackend([_final_round(ANSWER)])
        install_production_backend(backend.stream)

        assert harness.run_turn("What do you think of this project?")

        log = harness.worker_log_text()
        assert ANSWER not in log, f"answer leaked into the Worker Log: {log!r}"

    def test_conversational_turn_creates_no_fake_worker_activity(
        self, harness
    ) -> None:
        """A plain question ran no tools, so the Worker Log may stay empty."""
        backend = ScriptedBackend([_final_round(ANSWER)])
        install_production_backend(backend.stream)

        assert harness.run_turn("What do you think of this project?")

        # Only the workspace-owned completion receipt may appear; never prose.
        log = harness.worker_log_text()
        assert "Production Report" in log or log.strip() == ""
        assert ANSWER not in log

    def test_both_turns_return_after_restart(self, harness) -> None:
        backend = ScriptedBackend([_final_round(ANSWER)])
        install_production_backend(backend.stream)

        assert harness.run_turn("What do you think of this project?")

        loaded = harness.reload()
        assert [i["kind"] for i in loaded.chat_items] == [USER, PLANNER]
        assert loaded.chat_items[0]["text"] == "What do you think of this project?"
        assert loaded.chat_items[1]["text"] == ANSWER

        # And they render back into a fresh ChatView.
        from aura.gui.chat_view import ChatView

        fresh = ChatView()
        try:
            _render_into(fresh, loaded.chat_items)
            assert fresh.chat_items == loaded.chat_items
        finally:
            fresh.deleteLater()


# ── 4–6: implementation turns ───────────────────────────────────────────────


IMPL_SUMMARY = "Created notes.md with the project overview."


class TestImplementationTurn:
    def _script(self) -> list[list]:
        return [
            [
                ReasoningDelta(text="Looking at the repo before writing.\n"),
                *_tool_round(
                    [("w1", "write_file", {"path": "notes.md", "content": "# Notes\n"})],
                    text="Writing the notes file now. ",
                ),
            ],
            _final_round(IMPL_SUMMARY),
        ]

    def test_execution_activity_still_reaches_the_workspace(self, harness) -> None:
        backend = ScriptedBackend(self._script())
        install_production_backend(backend.stream)
        diffs: list[tuple] = []
        harness.bridge.workerDiffDecided.connect(lambda *a: diffs.append(a))

        assert harness.run_turn("Add a notes.md describing the project.")

        log = harness.worker_log_text()
        assert "Looking at the repo" in log, "reasoning must stay workspace-owned"
        assert "write_file" in log, f"tool activity missing from Worker Log: {log!r}"
        assert diffs, "the approved write must still project a diff"
        assert (harness.workspace / "notes.md").exists()

    def test_final_summary_reaches_chat_once_and_receipt_holds_facts(
        self, harness
    ) -> None:
        backend = ScriptedBackend(self._script())
        install_production_backend(backend.stream)
        finished: list[tuple] = []
        harness.bridge.workerFinished.connect(lambda *a: finished.append(a))

        assert harness.run_turn("Add a notes.md describing the project.")

        assert "".join(harness.chat_content).count(IMPL_SUMMARY) == 1
        assert IMPL_SUMMARY not in harness.worker_log_text()

        assert len(finished) == 1
        summary = finished[0][2]
        assert "notes.md" in summary, "the receipt must cite execution facts"
        assert IMPL_SUMMARY not in summary, (
            "the receipt must not restate the final answer"
        )

    def test_tool_rounds_do_not_duplicate_prose_or_record_early(
        self, harness
    ) -> None:
        """Proof 6: a tool-call round must not freeze a partial transcript.

        The round's pre-tool narration is suppressed outright (see
        ``tests/test_single_pre_tool_narration.py``), so the one recorded
        assistant turn is the final summary and nothing else.
        """
        backend = ScriptedBackend(self._script())
        install_production_backend(backend.stream)

        assert harness.run_turn("Add a notes.md describing the project.")

        assistant = harness.assistant_texts()
        assert len(assistant) == 1, f"expected one assistant turn, got {assistant}"
        recorded = assistant[0]
        assert "Writing the notes file now." not in recorded, (
            "pre-tool narration must never reach the chat transcript"
        )
        assert recorded.count(IMPL_SUMMARY) == 1
        # The completed turn survives the round trip.
        loaded = harness.reload()
        assert [i["kind"] for i in loaded.chat_items] == [USER, PLANNER]
        assert loaded.chat_items[1]["text"] == recorded

    def test_pre_tool_narration_is_not_persisted_in_history(self, harness) -> None:
        """It must also be absent from the messages replayed to the provider."""
        backend = ScriptedBackend(self._script())
        install_production_backend(backend.stream)

        assert harness.run_turn("Add a notes.md describing the project.")

        stored = json.dumps(harness.bridge.history.messages)
        assert "Writing the notes file now." not in stored
        tool_call_messages = [
            m for m in harness.bridge.history.messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        assert tool_call_messages, "the write round must still be in history"
        assert tool_call_messages[0]["content"] == ""
        assert tool_call_messages[0]["tool_calls"][0]["id"] == "w1"

    def test_no_worker_summary_card_stands_in_for_the_answer(self, harness) -> None:
        backend = ScriptedBackend(self._script())
        install_production_backend(backend.stream)

        assert harness.run_turn("Add a notes.md describing the project.")

        assert harness.chat._worker_summary_cards == {}, (
            "the execution receipt must not become a chat card"
        )


# ── 7: queued follow-up ─────────────────────────────────────────────────────


class TestQueuedFollowUp:
    def test_queued_conversational_followup_persists_after_chained_execution(
        self, harness
    ) -> None:
        first = "Wrote the file."
        second = "It is a small Qt project."
        backend = ScriptedBackend([
            _tool_round(
                [("w1", "write_file", {"path": "a.md", "content": "a\n"})],
                text="Working. ",
            ),
            _final_round(first),
            _final_round(second),
        ])
        install_production_backend(backend.stream)

        # The follow-up is queued while the first turn is still running, and
        # drains from the finished lifecycle exactly as MainWindow does.
        harness.queue.append("And what do you think of it?")

        finished_turns = {"count": 0}
        loop = QEventLoop()

        def _count() -> None:
            finished_turns["count"] += 1
            if finished_turns["count"] >= 2:
                loop.quit()

        harness.bridge.finished.connect(_count)
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(60000)

        harness.bridge.history.set_system("system")
        harness.send("Create a.md.")
        loop.exec()
        guard.stop()
        harness.settle()

        assert finished_turns["count"] == 2, "the queued turn never ran"
        assert harness.user_texts() == ["Create a.md.", "And what do you think of it?"]
        # "Working. " was the write round's pre-tool narration and is suppressed;
        # each turn's chat-owned prose is its final answer.
        assert harness.assistant_texts() == [first, second]

        loaded = harness.reload()
        assert [i["kind"] for i in loaded.chat_items] == [
            USER, PLANNER, USER, PLANNER,
        ]
        assert loaded.chat_items[-1]["text"] == second


# ── 8: cancellation ─────────────────────────────────────────────────────────


class TestCancellation:
    def test_cancellation_creates_no_fake_completed_assistant_turn(
        self, harness
    ) -> None:
        import threading

        entered = threading.Event()

        class _BlockingBackend:
            calls: list = []

            def stream(self, **kwargs):
                self.calls.append(kwargs)
                cancel_event = kwargs.get("cancel_event")
                entered.set()
                if cancel_event is not None:
                    cancel_event.wait(timeout=20)
                return iter(())

        install_production_backend(_BlockingBackend().stream)

        cancelled: list[str] = []
        finished: list[tuple] = []
        harness.bridge.workerCancelled.connect(lambda r: cancelled.append(r))
        harness.bridge.workerFinished.connect(lambda *a: finished.append(a))

        loop = QEventLoop()
        harness.bridge.finished.connect(loop.quit)
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(30000)

        harness.bridge.history.set_system("system")
        harness.send("do something long")
        assert entered.wait(timeout=10), "backend never started"
        harness.bridge.request_cancel()
        loop.exec()
        guard.stop()
        harness.settle()

        assert cancelled, "cancellation must reach the workspace"
        assert finished == [], "a cancelled run must not emit a completion receipt"
        # The user turn is real and is kept; no assistant turn is invented.
        assert harness.user_texts() == ["do something long"]
        assert harness.assistant_texts() == []
        assert harness.chat._worker_summary_cards == {}

        loaded = harness.reload()
        assert not any(i["kind"] == PLANNER for i in loaded.chat_items)


# ── handoff ordering ────────────────────────────────────────────────────────


class TestHandoffOrdering:
    def test_handoff_runs_after_the_finished_turn_is_saved(self) -> None:
        """A handoff abandons the conversation — the turn must be saved first.

        Auto-save now runs from the finished lifecycle, so handoff finalization
        moved there too; if the order flipped, the last turn would be lost.
        """
        from aura.gui.main_window import MainWindow

        window = MainWindow.__new__(MainWindow)
        order: list[str] = []

        class _Persistence:
            def auto_save(self, **_kwargs) -> None:
                order.append("save")

        class _Handoff:
            def finalize_handoff(self, message: dict) -> None:
                order.append(f"handoff:{message['content']}")

        window._persistence = _Persistence()
        window._handoff_controller = _Handoff()
        window._workspace_root = Path(".")
        window._settings = type(
            "S", (), {"provider": "p", "planner_provider": "p", "worker_provider": "p"}
        )()
        window.current_model = lambda: "m"
        window.current_thinking = lambda: "off"
        window.current_worker_model = lambda: "wm"
        window.current_worker_thinking = lambda: "off"
        window._final_stream_message = {"role": "assistant", "content": "HANDOFF"}

        window._settle_finished_turn()

        assert order == ["save", "handoff:HANDOFF"]
        # The message is consumed, so a later turn cannot replay the handoff.
        assert window._final_stream_message == {}
        window._settle_finished_turn()
        assert order == ["save", "handoff:HANDOFF", "save"]


# ── 9: errors survive a restart ─────────────────────────────────────────────


class TestErrorDurability:
    def test_api_error_is_visible_and_survives_save_reload(self, harness) -> None:
        class _FailingBackend:
            def stream(self, **kwargs):
                return iter([
                    ApiError(status_code=502, message="upstream unavailable"),
                ])

        install_production_backend(_FailingBackend().stream)

        assert harness.run_turn("Explain the bridge.")

        items = harness.chat.chat_items
        errors = [i for i in items if i["kind"] == ERROR]
        assert errors, f"the API error must be recorded, got {items}"
        assert errors[0]["title"] == "API Error 502"
        assert "upstream unavailable" in errors[0]["message"]

        loaded = harness.reload()
        reloaded_errors = [i for i in loaded.chat_items if i["kind"] == ERROR]
        assert reloaded_errors == errors

        # It renders again into a fresh view.
        from aura.gui.chat_view import ChatView

        fresh = ChatView()
        try:
            _render_into(fresh, loaded.chat_items)
            assert [i["kind"] for i in fresh.chat_items] == [
                i["kind"] for i in loaded.chat_items
            ]
        finally:
            fresh.deleteLater()
