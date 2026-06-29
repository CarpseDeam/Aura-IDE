from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import pytest

from aura.client.events import ContentDelta, Done, ToolResult, WorkerDispatchRequested
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.hooks import hooks
from aura.research.adapter import WEB_RESEARCH_DRONE_ID, execute_web_research_request
from aura.research.intent import classify_research_intent
from aura.research.policy import (
    ANSWER_ONLY,
    NO_RESEARCH,
    RESEARCH_THEN_WORKER,
    decide_research_policy,
)
from aura.research.request import build_research_request
from aura.research.result import ResearchResult


def _tool_call(id: str, name: str, args: dict) -> dict:
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _done(content: str = "", tool_calls: list[dict] | None = None) -> Done:
    message: dict = {
        "role": "assistant",
        "content": content,
        "reasoning_content": None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return Done(finish_reason="tool_calls" if tool_calls else "stop", full_message=message)


def _valid_dispatch_args() -> dict:
    return {
        "goal": "Answer the current-info question",
        "files": ["a.py"],
        "spec": "Use the researched facts to update a.py.",
        "acceptance": "Run python -m py_compile a.py.",
    }


def _pure_research_dispatch_args() -> dict:
    return {
        "goal": "Answer the current-info question",
        "files": [],
        "spec": "Research the current CEO and answer from sourced evidence.",
        "acceptance": "Answer the user with citations.",
    }


@pytest.mark.parametrize(
    "text",
    [
        "What is the latest Python version?",
        "Find the FastAPI docs example for dependency injection.",
        "What is the current OpenAI API pricing?",
        "What changed in the newest Nuitka changelog?",
        "What time are World Cup matches today?",
        "Who is the current CEO of Microsoft?",
        "Look up what ERR_HTTP2_PROTOCOL_ERROR means.",
        "Check https://example.com/docs for the current API reference.",
    ],
)
def test_research_intent_classifies_external_current_shapes(text: str) -> None:
    intent = classify_research_intent(text)

    assert intent.needs_research is True
    assert intent.category != "none"


@pytest.mark.parametrize(
    "text",
    [
        "Search this repo for the settings dialog.",
        "Read file aura/prompts.py.",
        "Inspect the workspace tree.",
        "What is the current git status?",
    ],
)
def test_local_repo_file_workspace_questions_are_no_research(text: str) -> None:
    decision = decide_research_policy(text)

    assert decision.route == NO_RESEARCH
    assert decision.intent.needs_research is False


@pytest.mark.parametrize(
    "text",
    [
        "Fix the create variations feature.",
        "Add a new unit test for the parser.",
        "Refactor the worker summary helper.",
    ],
)
def test_normal_coding_tasks_are_no_research(text: str) -> None:
    assert decide_research_policy(text).route == NO_RESEARCH


def test_hybrid_external_docs_plus_code_is_planner_guided_research_then_worker() -> None:
    decision = decide_research_policy(
        "Use the latest FastAPI docs to update our endpoint implementation."
    )

    assert decision.route == RESEARCH_THEN_WORKER
    assert decision.requires_research_first is True
    assert decision.planner_guided_worker_after_research is True
    assert decision.deterministically_enforced is False


def test_pure_external_question_routes_answer_only() -> None:
    decision = decide_research_policy("Who is the current CEO of Microsoft?")

    assert decision.route == ANSWER_ONLY
    assert decision.allow_worker_dispatch is False
    assert decision.deterministically_enforced is True


def test_research_request_preserves_user_question_cleanly() -> None:
    request = build_research_request("  What is the latest Python version?  \n")

    assert request.question == "What is the latest Python version?"
    assert request.original_text == "  What is the latest Python version?  \n"
    assert request.drone_id == WEB_RESEARCH_DRONE_ID


def test_research_result_normalizes_sample_web_research_receipt() -> None:
    result = ResearchResult.from_drone_receipt(
        {
            "ok": True,
            "run_id": "run-web",
            "drone_id": "web-research",
            "status": "completed",
            "cargo": {
                "answer": "Python 3.14.0 is the newest stable release.",
                "sources": [{"title": "Python Downloads", "url": "https://python.org"}],
                "evidence": [{"excerpt": "Python 3.14.0"}],
                "verified_facts": ["Python 3.14.0 is listed as stable."],
                "gaps": [],
                "confidence": "high",
                "trace": [{"step": "synthesize_answer"}],
                "route_used": {"type": "browser"},
            },
        }
    )

    assert result.ok is True
    assert result.answer.startswith("Python 3.14.0")
    assert result.sources[0]["url"] == "https://python.org"
    assert result.evidence[0]["excerpt"] == "Python 3.14.0"
    assert result.verified_facts == ["Python 3.14.0 is listed as stable."]
    assert result.confidence == "high"
    assert result.trace == [{"step": "synthesize_answer"}]
    assert result.route_used == {"type": "browser"}


def test_adapter_targets_web_research_through_existing_sync_seam(tmp_path: Path) -> None:
    request = build_research_request("What is the current OpenAI API pricing?")
    calls: list[dict] = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "drone_id": kwargs["drone_id"], "summary": "done"}

    result = execute_web_research_request(
        tmp_path,
        request,
        runner=fake_runner,
        drone_loader=lambda _root, drone_id: object(),
    )

    assert result["ok"] is True
    assert calls[0]["drone_id"] == "web-research"
    assert calls[0]["goal"] == "What is the current OpenAI API pricing?"
    assert calls[0]["upstream"]["research_request"]["drone_id"] == "web-research"


def test_pure_research_dispatch_to_worker_is_blocked(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("Who is the current CEO of Microsoft?")

    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="planner")

    manager = ConversationManager(history, tools)
    events = []
    dispatch_cb = MagicMock(
        return_value=WorkerDispatchResult(ok=True, summary="should not run")
    )
    backend = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[_tool_call("dispatch1", "dispatch_to_worker", _pure_research_dispatch_args())])]),
            iter([ContentDelta("Use web research instead."), _done("Use web research instead.")]),
        ]
    )

    hooks.register("generate_planner_code", backend)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
    finally:
        hooks.unregister("generate_planner_code")

    dispatch_cb.assert_not_called()
    assert not any(isinstance(event, WorkerDispatchRequested) for event in events)
    result = next(
        event
        for event in events
        if isinstance(event, ToolResult) and event.name == "dispatch_to_worker"
    )
    payload = json.loads(result.result)
    assert payload["extras"]["pure_research"] is True
    assert history.messages[-1]["content"] == "Use web research instead."


def test_screenshot_decompile_research_terms_do_not_block_local_dispatch(tmp_path: Path) -> None:
    history = History()
    user_text = (
        "[Image 1 structural decompile:]\n"
        "A panel mentions current version, release docs, and latest status.\n\n"
        "[User's question:]\n"
        "Dispatch is not working. The planner writes a spec and passes it to the worker."
    )
    assert decide_research_policy(user_text).route == ANSWER_ONLY
    history.append_user_text(user_text)

    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="planner")

    manager = ConversationManager(history, tools)
    events = []
    dispatch_cb = MagicMock(
        return_value=WorkerDispatchResult(ok=True, summary="worker ran")
    )
    args = {
        "goal": "Fix planner dispatch gate",
        "files": ["aura/conversation/manager.py"],
        "spec": "Update dispatch gating so local code work is not blocked by research classification.",
        "acceptance": "Run python -m py_compile aura/conversation/manager.py.",
    }
    backend = MagicMock(
        return_value=iter([
            _done(tool_calls=[_tool_call("dispatch1", "dispatch_to_worker", args)])
        ])
    )

    hooks.register("generate_planner_code", backend)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
    finally:
        hooks.unregister("generate_planner_code")

    dispatch_cb.assert_called_once()
    assert any(isinstance(event, WorkerDispatchRequested) for event in events)
    dispatch_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "dispatch_to_worker"
    ]
    assert dispatch_results
    assert not any(event.extras.get("pure_research") for event in dispatch_results)


def test_screenshot_decompile_allows_local_dispatch_when_path_is_only_in_spec(tmp_path: Path) -> None:
    history = History()
    user_text = (
        "[Image 1 structural decompile:]\n"
        "The panel says current version, release docs, and latest status.\n\n"
        "[User's question:]\n"
        "Dispatch is looping instead of reaching the worker."
    )
    assert decide_research_policy(user_text).route == ANSWER_ONLY
    history.append_user_text(user_text)

    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="planner")

    manager = ConversationManager(history, tools)
    events = []
    dispatch_cb = MagicMock(
        return_value=WorkerDispatchResult(ok=True, summary="worker ran")
    )
    args = {
        "goal": "Fix planner dispatch gate",
        "files": [],
        "spec": "Update aura/conversation/manager.py so local code dispatch is allowed.",
        "acceptance": "Run python -m py_compile aura/conversation/manager.py.",
    }
    backend = MagicMock(
        return_value=iter([
            _done(tool_calls=[_tool_call("dispatch1", "dispatch_to_worker", args)])
        ])
    )

    hooks.register("generate_planner_code", backend)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
    finally:
        hooks.unregister("generate_planner_code")

    dispatch_cb.assert_called_once()
    assert any(isinstance(event, WorkerDispatchRequested) for event in events)


def test_normal_local_extract_helper_dispatch_is_allowed(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("Extract the dispatch gate helper into a clearer local function.")

    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="planner")

    manager = ConversationManager(history, tools)
    events = []
    dispatch_cb = MagicMock(
        return_value=WorkerDispatchResult(ok=True, summary="worker ran")
    )
    args = {
        "goal": "Extract helper",
        "files": ["aura/conversation/manager.py"],
        "spec": "Extract the local dispatch gate logic into a helper function.",
        "acceptance": "Run python -m py_compile aura/conversation/manager.py.",
    }
    backend = MagicMock(
        return_value=iter([
            _done(tool_calls=[_tool_call("dispatch1", "dispatch_to_worker", args)])
        ])
    )

    hooks.register("generate_planner_code", backend)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
    finally:
        hooks.unregister("generate_planner_code")

    dispatch_cb.assert_called_once()
    assert any(isinstance(event, WorkerDispatchRequested) for event in events)


def test_repeated_pure_research_dispatch_block_stops_loop(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("Who is the current CEO of Microsoft?")

    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="planner")

    manager = ConversationManager(history, tools)
    events = []
    dispatch_cb = MagicMock(
        return_value=WorkerDispatchResult(ok=True, summary="should not run")
    )
    dispatch_call = _tool_call(
        "dispatch1",
        "dispatch_to_worker",
        _pure_research_dispatch_args(),
    )
    repeated_dispatch_call = _tool_call(
        "dispatch2",
        "dispatch_to_worker",
        _pure_research_dispatch_args(),
    )
    backend = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[dispatch_call])]),
            iter([_done(tool_calls=[repeated_dispatch_call])]),
            iter([ContentDelta("Should not be reached"), _done("Should not be reached")]),
        ]
    )

    hooks.register("generate_planner_code", backend)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
    finally:
        hooks.unregister("generate_planner_code")

    dispatch_cb.assert_not_called()
    assert backend.call_count == 2
    dispatch_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "dispatch_to_worker"
    ]
    assert len(dispatch_results) == 2
    assert all(event.extras.get("pure_research") for event in dispatch_results)
