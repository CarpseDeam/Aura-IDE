import json
import threading

from aura.conversation.history import History
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.research.policy import (
    ANSWER_ONLY,
    RESEARCH_THEN_WORKER,
    decide_research_policy,
)


def _round_runner(tmp_path, history: History | None = None) -> tuple[ToolRoundRunner, History]:
    history = history or History()
    loop_detector = LoopDetector()
    registry = ToolRegistry(tmp_path, mode="planner")
    tool_runner = ToolRunner(
        history,
        tmp_path,
        loop_detector,
        VerificationProgressTracker(),
    )
    return (
        ToolRoundRunner(
            history=history,
            tools=registry,
            tool_runner=tool_runner,
            loop_detector=loop_detector,
            planner_refresh=PlannerRefreshState(),
        ),
        history,
    )


def _dispatch_tool_call(args: dict, call_id: str = "call_dispatch") -> dict:
    return {
        "id": call_id,
        "function": {
            "name": "dispatch_to_worker",
            "arguments": json.dumps(args),
        },
    }


def _valid_dispatch_args() -> dict:
    return {
        "goal": "Update the local send handler with current research behavior.",
        "files": ["aura/gui/send_handler.py"],
        "spec": "Update aura/gui/send_handler.py after researching current World Cup schedule behavior.",
        "acceptance": "The local send handler handles the researched behavior.",
        "summary": "Update send handler behavior.",
        "steps": [
            {
                "id": "step-1",
                "title": "Send handler update",
                "goal": "Update the local send handler code.",
                "spec": "Modify aura/gui/send_handler.py with the researched behavior.",
                "files": ["aura/gui/send_handler.py"],
                "acceptance": "The send handler handles the researched behavior.",
            }
        ],
    }


def test_answer_only_research_blocks_worker_dispatch(tmp_path):
    runner, history = _round_runner(tmp_path)
    state = _SendState(
        mode="planner",
        research_policy=decide_research_policy("Are there any World Cup matches today?"),
    )
    tool_call = _dispatch_tool_call(
        {
            "goal": "Answer whether there are World Cup matches today.",
            "spec": "Use current web evidence.",
            "acceptance": "Answer the question.",
            "summary": "Answer current schedule question.",
        }
    )
    dispatch_calls = []
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )

    outcome = runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=lambda event: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda tool_id, req: dispatch_calls.append((tool_id, req)),
        cleanup_cancelled=lambda on_event: None,
    )

    assert dispatch_calls == []
    assert outcome.action == "return"
    assert state.research_policy.route == ANSWER_ONLY


def test_hybrid_research_then_worker_may_dispatch_after_research(tmp_path):
    policy = decide_research_policy(
        "Look up online the current World Cup match schedule and update aura/gui/send_handler.py."
    )
    runner, history = _round_runner(tmp_path)
    state = _SendState(mode="planner", research_policy=policy)
    tool_call = _dispatch_tool_call(_valid_dispatch_args())
    dispatch_calls = []

    def _dispatch_cb(tool_id, req):
        dispatch_calls.append((tool_id, req))
        return WorkerDispatchResult(ok=True, summary="Worker dispatched.")

    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )

    runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=lambda event: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=_dispatch_cb,
        cleanup_cancelled=lambda on_event: None,
    )

    assert policy.route == RESEARCH_THEN_WORKER
    assert policy.allow_worker_dispatch is True
    assert len(dispatch_calls) == 1
