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
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch, is_recoverable_pre_worker_failure
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.spec_quality import (
    validate_planner_dispatch,
    validate_worker_dispatch_spec,
)
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
    dispatch_attempt = state.planner_dispatch.begin_attempt()
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
        if _dispatch_reached_visible_worker(result):
            _mark_dispatch_accepted(state)
        return _dispatch_result_to_round_result(
            tool_call_id=tool_call_id,
            result=result,
            state=state,
            workflow_state_cb=workflow_state_cb,
            args=args,
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
        return {
            "id": tool_call_id,
            "skip": True,
            "completed_dispatch_for_final": False,
            "terminal_dispatch": False,
        }

    # ── Planner dispatch scope validation ─────────────────────
    if state.mode in {"planner", "single"}:
        schema_quality = validate_worker_dispatch_spec(
            str(args.get("spec") or "") if isinstance(args, dict) else "",
            str(args.get("acceptance") or "") if isinstance(args, dict) else "",
            goal=str(args.get("goal") or "") if isinstance(args, dict) else "",
        )
        if not isinstance(args, dict):
            schema_quality.errors.insert(0, "dispatch arguments must be a JSON object")
            schema_quality.ok = False
        if not schema_quality.ok:
            return _synthetic_recoverable_dispatch_failure(
                context=context,
                tool_call_id=tool_call_id,
                args=args if isinstance(args, dict) else {},
                state=state,
                on_event=on_event,
                workflow_state_cb=workflow_state_cb,
                failure_class="dispatch_schema_rejected",
                summary="Planner dispatch arguments failed schema validation.",
                errors=schema_quality.errors,
            )

        latest_user_text = _get_latest_user_text(context.history)
        quality = validate_planner_dispatch(args, latest_user_text)
        if not quality.ok:
            return _synthetic_recoverable_dispatch_failure(
                context=context,
                tool_call_id=tool_call_id,
                args=args,
                state=state,
                on_event=on_event,
                workflow_state_cb=workflow_state_cb,
                failure_class="planner_dispatch_scope_incomplete",
                summary="Planner dispatch scope incomplete.",
                errors=quality.errors,
                failure_constraint=quality.failure_constraint,
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
        _mark_dispatch_accepted(state)
    return _dispatch_result_to_round_result(
        tool_call_id=tool_call_id,
        result=result,
        state=state,
        workflow_state_cb=workflow_state_cb,
        args=args,
    )


def handle_invalid_dispatch_arguments_round(
    *,
    context: DispatchToolRoundContext,
    tool_call_id: str,
    raw_arguments: str,
    error: str,
    state: _SendState,
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None,
    on_event: EventCallback,
) -> dict[str, Any]:
    """Route malformed JSON through the same canonical recovery owner."""
    state.planner_dispatch.begin_attempt()
    return _synthetic_recoverable_dispatch_failure(
        context=context,
        tool_call_id=tool_call_id,
        args={},
        state=state,
        on_event=on_event,
        workflow_state_cb=workflow_state_cb,
        failure_class="malformed_dispatch_arguments",
        summary="Planner dispatch arguments were malformed JSON.",
        errors=[error],
        extra_payload={"raw_arguments": raw_arguments},
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
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a WorkerDispatchResult to the round result dict shape.

    Preserves existing behaviour: terminal dispatch detection, blocker
    construction, planner-side constraint propagation, attempt-brief
    attachment, and stale-read-file forwarding.
    """
    terminal_dispatch = False
    if result is not None and result.cancelled:
        return {
            "id": tool_call_id,
            "skip": True,
            "completed_dispatch_for_final": False,
            "terminal_dispatch": True,
        }
    if result is not None and not result.cancelled:
        if result.ok:
            terminal_dispatch = True
        else:
            extras = result.extras if isinstance(result.extras, dict) else {}
            failure_constraint = str(extras.get("failure_constraint") or "")
            attempt_brief = build_attempt_brief(result)
            if is_recoverable_pre_worker_failure(result):
                state.planner_dispatch.record_pre_worker_failure(result)
                _project_repairing_state(
                    workflow_state_cb,
                    tool_call_id=tool_call_id,
                    args=args or {},
                )
                if state.planner_dispatch.exhausted:
                    terminal_reason = state.planner_dispatch.exhaustion_reason()
                    _project_workflow_state(
                        workflow_state_cb,
                        tool_call_id=tool_call_id,
                        args=args or {},
                        status=WorkflowStatus.failed_nonrecoverable,
                    )
                    exhausted = WorkerDispatchResult(
                        ok=False,
                        summary=terminal_reason,
                        recoverable=False,
                        extras={
                            "dispatch_not_started": True,
                            "recovery_exhausted": True,
                            "failure_class": "planner_dispatch_recovery_exhausted",
                            "final_failure": state.planner_dispatch.last_failure_payload,
                        },
                    )
                    return {
                        "id": tool_call_id,
                        "blocker": True,
                        "result": exhausted,
                        "blocker_reason": "recovery_exhausted",
                        "failure_constraint": "",
                        "terminal_reason": terminal_reason,
                        "terminal_dispatch": False,
                    }
                d = {
                    "id": tool_call_id,
                    "skip": True,
                    "completed_dispatch_for_final": False,
                    "terminal_dispatch": False,
                    "planner_internal_constraint": state.planner_dispatch.corrective_message(),
                    "enter_silent_preflight": True,
                }
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


def _mark_dispatch_accepted(state: _SendState) -> None:
    state.planner_dispatch.mark_accepted()
    state.limits.record_dispatch_accepted()


def _synthetic_recoverable_dispatch_failure(
    *,
    context: DispatchToolRoundContext,
    tool_call_id: str,
    args: dict[str, Any],
    state: _SendState,
    on_event: EventCallback,
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None,
    failure_class: str,
    summary: str,
    errors: list[str],
    failure_constraint: str = "",
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exact_errors = [str(error) for error in errors if str(error)]
    constraint = failure_constraint or (
        "CONSTRAINT FOR NEXT PLANNER ATTEMPT: Correct dispatch_to_worker: "
        + "; ".join(exact_errors)
    )
    extras = {
        "internal_planner_handoff": True,
        "failure_constraint": constraint,
        "dispatch_not_started": True,
        "recoverable": True,
        "user_visible_blocker": False,
        "dispatch_spec_rejected": True,
        "failure_class": failure_class,
        "quality_errors": exact_errors,
        **(extra_payload or {}),
    }
    if failure_class == "planner_dispatch_scope_incomplete":
        extras["planner_dispatch_scope_incomplete"] = True
    result = WorkerDispatchResult(
        ok=False,
        summary=summary,
        recoverable=True,
        extras=extras,
    )
    payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
    context.history.append_tool_result(tool_call_id, payload)
    on_event(
        ToolResult(
            tool_call_id=tool_call_id,
            name="dispatch_to_worker",
            ok=True,
            result=payload,
            extras={"dispatch": True, **extras},
        )
    )
    return _dispatch_result_to_round_result(
        tool_call_id=tool_call_id,
        result=result,
        state=state,
        workflow_state_cb=workflow_state_cb,
        args=args,
    )


def _project_repairing_state(
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None,
    *,
    tool_call_id: str,
    args: dict[str, Any],
) -> None:
    _project_workflow_state(
        workflow_state_cb,
        tool_call_id=tool_call_id,
        args=args,
        status=WorkflowStatus.planner_resolving,
    )


def _project_workflow_state(
    workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None,
    *,
    tool_call_id: str,
    args: dict[str, Any],
    status: WorkflowStatus,
) -> None:
    if workflow_state_cb is None:
        return
    workflow_state_cb(
        tool_call_id,
        str(args.get("goal") or "Repair Worker plan"),
        str(args.get("summary") or ""),
        status,
    )


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


__all__ = [
    "DispatchToolRoundContext",
    "handle_dispatch_to_worker_round",
    "handle_invalid_dispatch_arguments_round",
]
