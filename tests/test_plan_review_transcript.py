"""Resolved Plan Review transcript items: normalize, persist, and replay."""
from __future__ import annotations

import json
from pathlib import Path

from aura.conversation.chat_transcript import (
    PLAN_REVIEW,
    normalize_chat_item,
    normalize_chat_items,
    plan_review_item,
)
from aura.conversation.history import History
from aura.conversation.persistence import load_conversation, save_conversation
from aura.gui.conv_persistence import ConversationPersistence


def _item(**overrides) -> dict:
    base = plan_review_item(
        goal="Fix the parser",
        files=["a.py", "b.py"],
        spec="Rewrite the tokenizer loop.",
        acceptance="Existing parser tests pass.",
        summary="Rewrite tokenizer",
        approved=True,
        user_edited=True,
    )
    base.update(overrides)
    return base


def test_plan_review_item_shape() -> None:
    item = _item()
    assert item["kind"] == PLAN_REVIEW
    assert item["files"] == ["a.py", "b.py"]
    assert item["approved"] is True
    assert item["user_edited"] is True


def test_normalize_round_trips_a_resolved_plan_review_item() -> None:
    item = _item()
    normalized = normalize_chat_item(item)
    assert normalized == item


def test_normalize_rejects_malformed_plan_review_item() -> None:
    # Missing/invalid types should still normalize to safe defaults rather
    # than raising or silently keeping garbage.
    item = {"kind": PLAN_REVIEW, "files": "not-a-list"}
    normalized = normalize_chat_item(item)
    assert normalized["files"] == []
    assert normalized["goal"] == ""
    assert normalized["approved"] is False


def test_normalize_chat_items_keeps_plan_review_among_other_kinds() -> None:
    items = [
        {"kind": "user", "text": "hi"},
        _item(approved=False, user_edited=False),
        {"kind": "planner", "text": "done"},
    ]
    normalized = normalize_chat_items(items)
    assert [i["kind"] for i in normalized] == ["user", PLAN_REVIEW, "planner"]
    assert normalized[1]["approved"] is False


def test_save_and_load_conversation_persists_plan_review_item(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("hello")
    chat_items = [{"kind": "user", "text": "hello"}, _item()]

    path = save_conversation(history, tmp_path, model="model", thinking="off", chat_items=chat_items)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["chat_items"] == chat_items

    loaded = load_conversation(path)
    assert loaded.chat_items == chat_items


class _FakeChat:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self._items: list[dict] = []
        self._cards: list["_FakeCard"] = []

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

    def add_user(self, text: str, image_b64s=None) -> None:
        self.events.append(("user", text, image_b64s))

    def begin_assistant(self) -> object:
        self.events.append(("begin_assistant",))
        return object()

    def append_content(self, text: str) -> None:
        self.events.append(("planner", text))

    def assistant_done(self) -> None:
        self.events.append(("assistant_done",))

    def add_error(self, title, message, show_retry=False) -> None:
        self.events.append(("error", title, message, show_retry))

    def add_plan_review_card(self, review_id, goal, files, spec, acceptance, summary):
        card = _FakeCard(review_id, goal, files, spec, acceptance, summary)
        self._cards.append(card)
        self.events.append(("plan_review_card", review_id, goal, files, spec, acceptance, summary))
        return card


class _FakeCard:
    def __init__(self, review_id, goal, files, spec, acceptance, summary) -> None:
        self.review_id = review_id
        self.goal = goal
        self.files = files
        self.spec = spec
        self.acceptance = acceptance
        self.summary = summary
        self.resolved: bool | None = None

    def show_resolved(self, *, approved: bool) -> None:
        self.resolved = approved


def _conversation_persistence_with_chat(chat: _FakeChat) -> ConversationPersistence:
    cp = ConversationPersistence.__new__(ConversationPersistence)
    cp._chat = chat
    cp._active_replay_id = 0
    return cp


def test_replay_renders_a_resolved_non_interactive_card_and_never_executes() -> None:
    chat_items = [
        {"kind": "user", "text": "u"},
        _item(approved=True, user_edited=False),
        {"kind": "planner", "text": "Implemented."},
    ]
    chat = _FakeChat()
    cp = _conversation_persistence_with_chat(chat)
    cp._render_chat_items(chat_items)

    assert chat._cards, "replay must render the card, not skip it"
    card = chat._cards[0]
    assert card.goal == "Fix the parser"
    assert card.files == ["a.py", "b.py"]
    assert card.resolved is True, "replay must show the resolved state, never a pending review"

    assert ("begin_assistant",) in chat.events
    assert ("assistant_done",) in chat.events


def test_replay_renders_cancelled_plan_review_as_not_approved() -> None:
    chat_items = [_item(approved=False, user_edited=False)]
    chat = _FakeChat()
    cp = _conversation_persistence_with_chat(chat)
    cp._render_chat_items(chat_items)

    assert chat._cards[0].resolved is False
