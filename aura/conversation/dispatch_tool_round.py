"""Dispatch-to-worker round handling — Phase 4 extraction from manager_tool_round.py."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.client import Event, ToolResult
from aura.conversation.attempt_brief import build_attempt_brief
from aura.conversation.completion_guard import worker_dispatch_is_terminal
from aura.conversation.dispatch import DispatchCallback, WorkerDispatchResult
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.spec_quality import validate_planner_dispatch
from aura.conversation.workflow_state import WorkflowStatus
from aura.research.policy import ANSWER_ONLY

EventCallback = Callable[[Event], None]

_LOCAL_CODE_INTENT_RE = re.compile(
    r"\b(?:fix|add|update|change|modify|edit|patch|refactor|extract|move|"
    r"create|remove|delete|rename|implement|test|py_compile|pytest|import|"
    r"module|function|class|file)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DispatchToolRoundContext:
    """Dependencies needed to handle a dispatch_to_worker tool round.

    Contains runner-level dependencies (not per-call parameters).
    """

    history: History
    tool_runner: Any


def handle_dispatch_to_worker_round(
    *,
    context: DispatchToolRoundContext,
    tool_call_id: str,
    args: dict[str, Any],
    state: _SendState,
    dispatch_cb: DispatchCallback | None,
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
    on_event: EventCallback,
) -> dict[str, Any]:
    """Handle a ``dispatch_to_worker`` tool round.

    Preserves existing behaviour:
    - Pure research dispatch blocking for ANSWER_ONLY policy with no local code intent.
    - Planner dispatch scope validation.
    - Repeated dispatch handling when ``planner_visible_dispatch_tool_call_id`` is set.
    - Dispatch result conversion and stale-read-file propagation.
    """
    state.planner_dispatch_attempts += 1
    dispatch_attempt = state.planner_dispatch_attempts
    previous_dispatch_tool_call_id = state.planner_visible_dispatch_tool_call_id
    if previous_dispatch_tool_call_id:
        result = context.tool_runner.handle_dispatch(
            tool_call_id=tool_call_id,
            args=args,
            on_event=on_event,
            dispatch_cb=dispatch_cb,
            workflow_state_cb=workflow_state_cb,
            planner_dispatch_attempt=dispatch_attempt,
            previous_dispatch_tool_call_id=previous_dispatch_tool_call_id,
        )
        return _dispatch_result_to_round_result(
            tool_call_id=tool_call_id,
            result=result,
            state=state,
        )

    if (
        state.research_policy.route == ANSWER_ONLY
        and not _dispatch_args_look_like_local_code_work(args)
    ):
        result = _append_pure_research_dispatch_block(
            history=context.history,
            tool_call_id=tool_call_id,
            on_event=on_event,
        )
        action = classify_failed_worker_dispatch(
            result=result,
        )
        blocker_reason = action["blocker_reason"]
        failure_constraint = action.get("failure_constraint", "")
        if blocker_reason or failure_constraint:
            return {
                "id": tool_call_id,
                "blocker": True,
                "result": result,
                "blocker_reason": blocker_reason,
                "terminal_dispatch": False,
                "failure_constraint": failure_constraint,
            }
        return {
            "id": tool_call_id,
            "skip": True,
            "completed_dispatch_for_final": False,
            "terminal_dispatch": False,
        }

    # ── Planner dispatch scope validation ─────────────────────
    if state.mode in {"planner", "single"}:
        latest_user_text = _get_latest_user_text(context.history)
        quality = validate_planner_dispatch(args, latest_user_text)
        if not quality.ok:
            result = WorkerDispatchResult(
                ok=False,
                summary="Planner dispatch scope incomplete.",
                recoverable=True,
                extras={
                    "internal_planner_handoff": True,
                    "failure_constraint": quality.failure_constraint,
                    "dispatch_not_started": True,
                    "user_visible_blocker": False,
                    "planner_dispatch_scope_incomplete": True,
                },
            )
            return _dispatch_result_to_round_result(
                tool_call_id=tool_call_id,
                result=result,
                state=state,
            )

    result = context.tool_runner.handle_dispatch(
        tool_call_id=tool_call_id,
        args=args,
        on_event=on_event,
        dispatch_cb=dispatch_cb,
        workflow_state_cb=workflow_state_cb,
        planner_dispatch_attempt=dispatch_attempt,
        previous_dispatch_tool_call_id=previous_dispatch_tool_call_id,
    )
    if _dispatch_reached_visible_worker(result):
        state.planner_visible_dispatch_tool_call_id = tool_call_id
    return _dispatch_result_to_round_result(
        tool_call_id=tool_call_id,
        result=result,
        state=state,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _append_pure_research_dispatch_block(
    *,
    history: History,
    tool_call_id: str,
    on_event: EventCallback,
) -> WorkerDispatchResult:
    """Emit a pure research dispatch block and return the result."""
    summary = (
        "Worker was not started because this turn is a pure external "
        "research request. Run web research and answer from sourced evidence."
    )
    result = WorkerDispatchResult(
        ok=False,
        summary=summary,
        recoverable=True,
        extras={
            "dispatch_not_started": True,
            "pure_research": True,
            "research_route": "answer_only",
        },
    )
    payload = json.dumps(
        result.to_tool_payload(),
        ensure_ascii=False,
    )
    history.append_tool_result(tool_call_id, payload)
    on_event(
        ToolResult(
            tool_call_id=tool_call_id,
            name="dispatch_to_worker",
            ok=True,
            result=payload,
            extras={
                "dispatch_not_started": True,
                "pure_research": True,
                "recoverable": True,
                "summary": summary,
            },
        )
    )
    return result


def _dispatch_result_to_round_result(
    *,
    tool_call_id: str,
    result: WorkerDispatchResult | None,
    state: _SendState,
) -> dict[str, Any]:
    """Convert a WorkerDispatchResult to the round result dict shape.

    Preserves existing behaviour: terminal dispatch detection, blocker
    construction, planner-side constraint propagation, attempt-brief
    attachment, and stale-read-file forwarding.
    """
    terminal_dispatch = False
    if result is not None and not result.cancelled:
        if result.ok:
            terminal_dispatch = True
        else:
            extras = result.extras if isinstance(result.extras, dict) else {}
            failure_constraint = str(extras.get("failure_constraint") or "")
            attempt_brief = build_attempt_brief(result)
            if extras.get("internal_planner_handoff") and failure_constraint:
                if failure_constraint in state.seen_internal_constraints:
                    d = {
                        "id": tool_call_id,
                        "blocker": True,
                        "result": result,
                        "blocker_reason": "failed",
                        "failure_constraint": failure_constraint,
                        "terminal_dispatch": False,
                    }
                    if attempt_brief is not None:
                        d["attempt_brief"] = attempt_brief
                    return d
                state.seen_internal_constraints.add(failure_constraint)
                d = {
                    "id": tool_call_id,
                    "skip": True,
                    "completed_dispatch_for_final": False,
                    "terminal_dispatch": False,
                    "planner_internal_constraint": failure_constraint,
                    "enter_silent_preflight": True,
                }
                if attempt_brief is not None:
                    d["attempt_brief"] = attempt_brief
                return d
            action = classify_failed_worker_dispatch(
                result=result,
            )
            blocker_reason = action["blocker_reason"]
            failure_constraint = action.get("failure_constraint", "")
            if blocker_reason or failure_constraint:
                d = {
                    "id": tool_call_id,
                    "blocker": True,
                    "result": result,
                    "blocker_reason": blocker_reason,
                    "failure_constraint": failure_constraint,
                    "planner_stale_read_files": (
                        list(result.modified_files)
                        if result.modified_files
                        else []
                    ),
                    "terminal_dispatch": False,
                }
                if attempt_brief is not None:
                    d["attempt_brief"] = attempt_brief
                return d
    return {
        "id": tool_call_id,
        "skip": True,
        "completed_dispatch_for_final": worker_dispatch_is_terminal(result),
        "planner_stale_read_files": (
            list(result.modified_files) if result and result.modified_files else []
        ),
        "terminal_dispatch": terminal_dispatch,
    }


def _dispatch_reached_visible_worker(result: WorkerDispatchResult | None) -> bool:
    """Return True if the dispatch result indicates a visible worker was reached."""
    if result is None:
        return False
    extras = result.extras if isinstance(result.extras, dict) else {}
    return not bool(extras.get("dispatch_not_started"))


def _get_latest_user_text(history: History) -> str:
    """Extract the most recent user message text from history."""
    for message in reversed(history.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
    return ""


def _dispatch_args_look_like_local_code_work(args: dict[str, Any]) -> bool:
    """Heuristic: do the dispatch args reference local code work?"""
    if not isinstance(args, dict):
        return False

    if not any(_looks_like_local_path(path) for path in _dispatch_local_path_candidates(args)):
        return False

    intent_text = " ".join(
        str(args.get(key) or "")
        for key in ("spec", "goal", "acceptance")
    )
    return bool(_LOCAL_CODE_INTENT_RE.search(intent_text))


def _dispatch_local_path_candidates(args: dict[str, Any]) -> list[Any]:
    """Collect candidate path strings from dispatch args."""
    candidates: list[Any] = []

    files = args.get("files")
    if isinstance(files, list):
        candidates.extend(files)

    target_regions = args.get("target_regions")
    if isinstance(target_regions, list):
        for region in target_regions:
            if isinstance(region, dict):
                candidates.append(region.get("path"))

    required_outputs = args.get("required_outputs")
    if isinstance(required_outputs, list):
        candidates.extend(required_outputs)

    for key in ("spec", "goal", "acceptance", "summary"):
        candidates.extend(_extract_local_path_mentions(str(args.get(key) or "")))

    return candidates


def _extract_local_path_mentions(text: str) -> list[str]:
    """Extract likely file/directory path mentions from free text."""
    mentions: list[str] = []
    for match in re.finditer(
        r"(?<![\w:/.-])([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+)(?![\w.-])",
        text,
    ):
        mentions.append(match.group(1))
    for match in re.finditer(
        r"(?<![\w.-])([A-Za-z0-9_.-]+\."
        r"(?:py|pyw|ts|tsx|js|jsx|json|toml|yaml|yml|md|txt|css|scss|html|"
        r"gd|cs|java|go|rs|cpp|c|h|hpp))(?![\w.-])",
        text,
    ):
        mentions.append(match.group(1))
    return mentions


def _looks_like_local_path(value: Any) -> bool:
    """Heuristic: does *value* resemble a local file-system path?"""
    if not isinstance(value, str):
        return False
    path = value.strip()
    if not path or "\n" in path or "\r" in path:
        return False
    lowered = path.lower()
    if "://" in lowered or lowered.startswith(("www.", "http:", "https:")):
        return False
    return (
        "/" in path
        or "\\" in path
        or "." in Path(path).name
        or path.startswith(".")
    )
