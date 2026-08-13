"""The production model continues through tool results in one conversation."""

from __future__ import annotations

import threading

from aura.client import ContentDelta, Done, ToolCallStart, ToolResult
from aura.conversation import ConversationManager, History
from aura.conversation.tools import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams


def test_one_model_continues_after_a_checklist_update(tmp_path) -> None:
    history = History()
    history.append_user_text("Inspect, change, and validate the request.")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    calls: list[dict] = []

    def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            yield ToolCallStart(index=0, id="checklist-1", name="update_task_checklist")
            yield Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "checklist-1",
                            "type": "function",
                            "function": {
                                "name": "update_task_checklist",
                                "arguments": (
                                    '{"items":[{"id":"inspect","text":"Inspect the request",'
                                    '"status":"active"}]}'
                                ),
                            },
                        }
                    ],
                },
            )
            return
        yield ContentDelta("Completed from the accumulated conversation.")
        yield Done(
            finish_reason="stop",
            full_message={
                "role": "assistant",
                "content": "Completed from the accumulated conversation.",
            },
        )

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    events: list[object] = []
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: None,
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)

    assert len(calls) == 2
    assert calls[0]["model"] == calls[1]["model"] == "test-model"
    assert any(isinstance(event, ToolResult) and event.ok for event in events)
    assert history.messages[-1]["content"] == "Completed from the accumulated conversation."
