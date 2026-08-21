"""Legacy chat transcript migration: the retired "Needs follow-up" card.

Production runs used to synthesize a generic "Needs follow-up" error card in
chat whenever a receipt carried ``needs_followup``. That presentation is
removed (see execution_finish_presenter.py / execution_finish_outcome.py),
but old persisted conversations may still contain an ``error`` chat item
titled exactly "Needs follow-up". Loading such a conversation must silently
drop that one item while leaving every other historical error intact.
"""
from __future__ import annotations

from aura.conversation.chat_transcript import (
    ERROR,
    normalize_chat_item,
    normalize_chat_items,
)


def test_legacy_needs_followup_error_item_is_dropped_on_normalize() -> None:
    raw = {"kind": ERROR, "title": "Needs follow-up", "message": "Validation failed: x"}

    assert normalize_chat_item(raw) is None


def test_legacy_needs_followup_error_item_is_dropped_from_a_list() -> None:
    raw_items = [
        {"kind": "user", "text": "hi"},
        {"kind": ERROR, "title": "Needs follow-up", "message": "Validation failed: x"},
        {"kind": "assistant", "text": "done"},
    ]

    items = normalize_chat_items(raw_items)

    assert [item["kind"] for item in items] == ["user", "assistant"]


def test_unrelated_persisted_errors_are_not_hidden() -> None:
    raw_items = [
        {"kind": ERROR, "title": "API Error 500", "message": "boom"},
        {"kind": ERROR, "title": "Needs follow-up", "message": "Validation failed: x"},
        {"kind": ERROR, "title": "Execution Error", "message": "harness exploded"},
        {"kind": ERROR, "title": "needs follow-up", "message": "different case, kept"},
    ]

    items = normalize_chat_items(raw_items)

    titles = [item["title"] for item in items]
    assert titles == ["API Error 500", "Execution Error", "needs follow-up"]
