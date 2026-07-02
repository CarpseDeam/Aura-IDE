from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.conversation.chat_transcript import worker_complete_item
from aura.conversation.history import History
from aura.conversation.persistence import (
    WorkerDispatchRecord,
    load_conversation,
    save_conversation,
)
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.gui.cards.dispatch_status_labels import worker_summary_status_label
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.worker_finish_presenter import WorkerFinishPresenter


class FakeChat:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.worker_summaries: list[dict] = []
        self._items: list[dict] = []

    @property
    def chat_items(self) -> list[dict]:
        return list(self._items)

    def begin_bulk_update(self) -> None:
        self.events.append(("begin_bulk",))

    def end_bulk_update(self) -> None:
        self.events.append(("end_bulk",))

    def begin_transcript_replay(self) -> None:
        self.events.append(("begin_replay",))

    def end_transcript_replay(self, items: list[dict]) -> None:
        self._items = list(items)
        self.events.append(("end_replay", list(items)))

    def add_user(self, text: str, image_b64s: list[str] | None = None) -> None:
        self.events.append(("user", text, image_b64s))

    def begin_assistant(self) -> object:
        self.events.append(("begin_assistant",))
        return object()

    def append_content(self, text: str) -> None:
        self.events.append(("planner", text))

    def assistant_done(self) -> None:
        self.events.append(("assistant_done",))

    def add_worker_summary(
        self,
        tool_call_id: str,
        goal: str,
        ok: bool,
        summary: str,
        *,
        needs_followup: bool = False,
        status: str | None = None,
        is_internal: bool = False,
    ) -> None:
        self.worker_summaries.append(
            {
                "tool_call_id": tool_call_id,
                "goal": goal,
                "ok": ok,
                "summary": summary,
                "needs_followup": needs_followup,
                "status": status,
                "is_internal": is_internal,
            }
        )
        self.events.append(("worker", tool_call_id, goal, ok, summary, needs_followup, status))

    def add_mismatch_resolution_card(self, *args, **kwargs) -> None:
        self.events.append(("mismatch", args, kwargs))

    def begin_planner_resolution_aura(self) -> None:
        self.events.append(("resolution_aura",))


class FakePlayground:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def stop_aura(self) -> None:
        self.events.append(("stop_aura",))

    def worker_finished(self, *args, **kwargs) -> None:
        self.events.append(("worker_finished", args, kwargs))

    def set_worker_running(self, value: bool) -> None:
        self.events.append(("set_worker_running", value))

    def finish_todo_list(self, *args, **kwargs) -> None:
        self.events.append(("finish_todo_list", args, kwargs))


class FakeSpecCard:
    def __init__(self, goal: str = "Fix the bug") -> None:
        self.goal = goal
        self.finished: list[tuple] = []

    def current_spec(self) -> tuple[str, list, str, str, str]:
        return self.goal, [], "", "", ""

    def worker_finished(self, ok: bool, summary: str, status: str | None = None) -> None:
        self.finished.append((ok, summary, status))


class FakeBridge:
    def __init__(self, history: History, dispatch_records: list[object]) -> None:
        self.history = history
        self.dispatch_records = dispatch_records


def _conversation_persistence_with_chat(chat: FakeChat) -> ConversationPersistence:
    cp = ConversationPersistence.__new__(ConversationPersistence)
    cp._chat = chat
    cp._active_replay_id = 0
    return cp


def test_save_conversation_persists_chat_items(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("hello")
    chat_items = [
        {"kind": "user", "text": "hello"},
        {"kind": "planner", "text": "shown answer"},
        worker_complete_item(
            tool_call_id="tc1",
            goal="Goal",
            summary="Summary",
            status=WorkerOutcomeStatus.completed.value,
            ok=True,
            needs_followup=False,
        ),
    ]

    path = save_conversation(
        history,
        tmp_path,
        model="model",
        thinking="off",
        chat_items=chat_items,
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["chat_items"] == chat_items
    assert data["messages"] == history.messages


def test_load_with_chat_items_renders_exact_items_and_no_extra_dispatch_cards(tmp_path: Path) -> None:
    chat_items = [
        {"kind": "user", "text": "u"},
        {"kind": "planner", "text": "p"},
        {
            "kind": "worker_complete",
            "tool_call_id": "visible",
            "goal": "Visible goal",
            "summary": "Visible summary",
            "status": "completed",
            "ok": True,
            "needs_followup": False,
        },
    ]
    path = tmp_path / ".aura" / "conversations" / "c.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "model": "model",
                "thinking": "off",
                "messages": [{"role": "user", "content": "runtime"}],
                "chat_items": chat_items,
                "worker_dispatches": [
                    {
                        "after_message_index": 0,
                        "tool_call_id": "diagnostic",
                        "spec": {"goal": "Diagnostic goal"},
                        "worker_history": [],
                        "result_summary": "Must not render",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_conversation(path)
    chat = FakeChat()
    cp = _conversation_persistence_with_chat(chat)
    cp._render_chat_items(loaded.chat_items)

    assert chat.events == [
        ("begin_bulk",),
        ("begin_replay",),
        ("user", "u", None),
        ("begin_assistant",),
        ("planner", "p"),
        ("assistant_done",),
        ("worker", "visible", "Visible goal", True, "Visible summary", False, "completed"),
        ("end_replay", chat_items),
        ("end_bulk",),
    ]
    assert [w["tool_call_id"] for w in chat.worker_summaries] == ["visible"]


def test_legacy_v2_load_skips_worker_dispatch_reconstruction(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "model": "model",
                "thinking": "off",
                "messages": [
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "p", "tool_calls": [{"id": "tc"}]},
                    {"role": "tool", "tool_call_id": "tc", "content": "tool"},
                    {"role": "user", "content": "internal", "aura_internal": True},
                ],
                "worker_dispatches": [
                    {
                        "after_message_index": 1,
                        "tool_call_id": "tc",
                        "spec": {"goal": "Do work"},
                        "worker_history": [],
                        "result_summary": "Old result",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_conversation(path)
    assert loaded.worker_dispatches
    assert loaded.chat_items == [
        {"kind": "user", "text": "u"},
        {"kind": "planner", "text": "p"},
    ]

    chat = FakeChat()
    cp = _conversation_persistence_with_chat(chat)
    cp._render_chat_items(loaded.chat_items)

    assert chat.worker_summaries == []
    assert ("user", "u", None) in chat.events
    assert ("planner", "p") in chat.events


def test_replay_history_does_not_render_dispatch_records() -> None:
    history = History()
    history.append_user_text("u")
    history.append_assistant({"role": "assistant", "content": "p"})
    dispatch_record = WorkerDispatchRecord(
        after_message_index=1,
        tool_call_id="tc",
        spec={"goal": "Diagnostic"},
        worker_history=[],
        result_summary="Must not render",
    )
    chat = FakeChat()
    cp = _conversation_persistence_with_chat(chat)
    cp._bridge = FakeBridge(history, [dispatch_record])

    cp.replay_history(synchronous=True)

    assert chat.worker_summaries == []
    assert ("user", "u", None) in chat.events
    assert ("planner", "p") in chat.events


def test_worker_finish_presenter_adds_one_card_for_normal_completion() -> None:
    chat = FakeChat()
    presenter = WorkerFinishPresenter(chat, FakePlayground())

    presenter.present(
        tool_call_id="tc",
        ok=True,
        summary="done",
        needs_followup=False,
        status=WorkerOutcomeStatus.completed.value,
        metadata={},
        active_workflow=None,
        spec_card=FakeSpecCard("Normal goal"),
    )

    assert len(chat.worker_summaries) == 1
    assert chat.worker_summaries[0]["goal"] == "Normal goal"
    assert chat.worker_summaries[0]["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "expected_label"),
    [
        (WorkerOutcomeStatus.harness_error.value, "Worker Error"),
        (WorkerOutcomeStatus.approval_rejected.value, "Changes rejected"),
        (WorkerOutcomeStatus.cancelled.value, "Cancelled"),
        (WorkerOutcomeStatus.completed.value, "Worker Report"),
        (WorkerOutcomeStatus.validation_failed.value, "Worker Report"),
        (WorkerOutcomeStatus.edit_mechanics_blocked.value, "Worker Report"),
        ("no_progress", "Worker Report"),
        (None, "Worker Report"),
    ],
)
def test_worker_complete_card_label_policy(status: str | None, expected_label: str) -> None:
    label, _color = worker_summary_status_label(
        status=status,
        ok=status != WorkerOutcomeStatus.harness_error.value,
        needs_followup=status in {WorkerOutcomeStatus.validation_failed.value, "no_progress"},
        summary="",
    )
    assert label == expected_label


def test_worker_finish_presenter_exceptional_statuses_add_one_labeled_card() -> None:
    for status, expected in [
        (WorkerOutcomeStatus.harness_error.value, "Worker Error"),
        (WorkerOutcomeStatus.approval_rejected.value, "Changes rejected"),
    ]:
        chat = FakeChat()
        presenter = WorkerFinishPresenter(chat, FakePlayground())
        presenter.present(
            tool_call_id=status,
            ok=False,
            summary="summary",
            needs_followup=True,
            status=status,
            metadata={},
            active_workflow=None,
            spec_card=FakeSpecCard(),
        )

        assert len(chat.worker_summaries) == 1
        label, _color = worker_summary_status_label(
            status=chat.worker_summaries[0]["status"],
            ok=chat.worker_summaries[0]["ok"],
            needs_followup=chat.worker_summaries[0]["needs_followup"],
            summary=chat.worker_summaries[0]["summary"],
        )
        assert label == expected


def test_forbidden_labels_do_not_appear_in_worker_complete_card_labels() -> None:
    forbidden = ("Failed", "Needs attention", "Completed")
    statuses = [
        None,
        WorkerOutcomeStatus.completed.value,
        WorkerOutcomeStatus.completed_with_caveats.value,
        WorkerOutcomeStatus.validation_failed.value,
        WorkerOutcomeStatus.edit_mechanics_blocked.value,
        WorkerOutcomeStatus.scope_mismatch.value,
        WorkerOutcomeStatus.needs_followup.value,
        WorkerOutcomeStatus.approval_rejected.value,
        WorkerOutcomeStatus.cancelled.value,
        WorkerOutcomeStatus.harness_error.value,
        "no_progress",
    ]

    for status in statuses:
        label, _color = worker_summary_status_label(
            status=status,
            ok=status not in {WorkerOutcomeStatus.harness_error.value},
            needs_followup=status in {
                WorkerOutcomeStatus.validation_failed.value,
                WorkerOutcomeStatus.needs_followup.value,
                "no_progress",
            },
            summary="",
        )
        assert not any(word in label for word in forbidden), (status, label)


def test_runtime_ui_artifacts_are_not_restored_on_normal_chat_load() -> None:
    chat = FakeChat()
    cp = _conversation_persistence_with_chat(chat)

    cp._render_chat_items([
        {"kind": "user", "text": "u"},
        {"kind": "planner", "text": "p"},
    ])

    event_names = [event[0] for event in chat.events]
    assert "worker_log" not in event_names
    assert "todo" not in event_names
    assert "activity" not in event_names
    assert "terminal" not in event_names
    assert "tool" not in event_names
