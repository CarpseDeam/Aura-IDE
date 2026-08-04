"""Tool-call round execution for ConversationManager."""
from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.client import Event, ToolResult
from aura.conversation import _edit_shapes
from aura.conversation.attempt_brief import render_for_planner
from aura.conversation.completion_guard import tool_result_completes_action
from aura.conversation.dispatch import DispatchCallback
from aura.conversation.dispatch_tool_round import (
    DispatchToolRoundContext,
    handle_dispatch_to_worker_round,
)
from aura.conversation._report_tools import REPORT_ALREADY_SATISFIED
from aura.conversation.history import History
from aura.conversation.manager_recovery import (
    update_worker_recovery_state,
    worker_recovery_block,
)
from aura.conversation.manager_send_state import _SendState
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.pre_edit_loop_guard import is_narrow_read, read_fingerprint
from aura.conversation.single_trajectory import (
    EditRecoveryEvent,
    RoundFacts,
    observation_targets,
)
from aura.conversation.terminal_tool_round import (
    handle_run_and_watch_round,
    handle_run_terminal_command_round,
)
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.tool_preflight import (
    decode_arguments,
    exposed_tool_schemas,
    preflight_structural,
    schema_errors,
)
from aura.conversation.tool_round_events import (
    ToolRoundEventsContext,
    append_dispatch_blocker_message,
    append_limit_tool_result,
)
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalCallback
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_pre_tool_gate import (
    WorkerPreToolGateContext,
    run_worker_pre_tool_gate,
)
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    is_recoverable_phase_boundary,
    parse_tool_payload,
)
from aura.conversation.workflow_state import WorkflowStatus
from aura.events import EventBus
from aura.lifecycle import LifecycleHooks
from aura.work_artifact.model import ValidationCommandSpec

EventCallback = Callable[[Event], None]


def _invalid_call_reason(invalid: dict[str, Any]) -> str:
    """Return the human reason an invalid preflight call was rejected."""
    kind = invalid.get("kind")
    if kind in ("parse", "structure", "schema", "exposure"):
        return str(invalid.get("error") or "invalid tool call")
    if kind == "limit":
        info = invalid.get("limit_info") or {}
        return str(info.get("reason") or "tool limit reached")
    if kind == "guard":
        info = invalid.get("repeat_info") or {}
        return str(info.get("reason") or "loop guard rejection")
    return "invalid call"


def _reject_tool_call_batch(
    *,
    tool_calls: list[dict[str, Any]],
    invalid: dict[str, Any],
    context: ToolRoundEventsContext,
    on_event: EventCallback,
) -> None:
    """Reject a whole tool-call batch without executing any call.

    Every rejected tool call receives exactly one paired ``ToolResult``: the
    invalid call gets its own rejection payload, and every valid sibling gets
    a coherent batch-rejection payload explaining that nothing ran, so an
    accepted prefix can never execute ahead of the call that vetoes the batch.

    The rejection is defensive: a malformed call may lack ``id`` or
    ``function.name``, so every access falls back to a placeholder that keeps
    the pairing one-result-per-call intact.
    """
    invalid_id = invalid.get("tool_call_id") or ""
    invalid_name = invalid.get("name") or "<unknown>"
    invalid_kind = invalid.get("kind") or "invalid"
    invalid_reason = _invalid_call_reason(invalid)
    invalid_error = str(invalid.get("error") or "invalid tool call")
    failure_class = str(
        invalid.get("failure_class")
        or _invalid_call_failure_class(invalid_kind)
    )
    for index, tc in enumerate(tool_calls):
        fn = tc.get("function") if isinstance(tc, dict) else None
        name = (
            str(fn.get("name")) if isinstance(fn, dict) and fn.get("name") else "<unknown>"
        )
        tool_call_id = (
            tc.get("id")
            if isinstance(tc, dict)
            and isinstance(tc.get("id"), str)
            and tc.get("id")
            else f"__malformed_call_{index}__"
        )
        if tool_call_id == invalid_id:
            if invalid_kind == "limit":
                append_limit_tool_result(
                    context=context,
                    tool_call_id=tool_call_id,
                    name=name,
                    info=invalid["limit_info"],
                    on_event=on_event,
                )
            elif invalid_kind == "guard":
                append_limit_tool_result(
                    context=context,
                    tool_call_id=tool_call_id,
                    name=name,
                    info=invalid["repeat_info"],
                    on_event=on_event,
                )
            else:
                payload = json.dumps(
                    {
                        "ok": False,
                        "error": invalid_error,
                        "recoverable": True,
                        "failure_class": failure_class,
                        "tool": name,
                        "call_rejected": True,
                        "reason": "tool_call_invalid_before_execution",
                        "message": (
                            f"No call in this batch executed. Call {tool_call_id} "
                            f"({name}) was invalid before execution "
                            f"({invalid_reason}), so the whole batch was rejected. "
                            "Re-issue the corrected call(s)."
                        ),
                    },
                    ensure_ascii=False,
                )
                context.history.append_tool_result(tool_call_id, payload)
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=False,
                        result=payload,
                        extras={
                            "call_rejected": True,
                            "reason": "tool_call_invalid_before_execution",
                            "failure_class": failure_class,
                        },
                    )
                )
            continue
        payload = json.dumps(
            {
                "ok": False,
                "recoverable": True,
                "reason": "tool_batch_rejected_before_execution",
                "tool": name,
                "batch_rejected": True,
                "rejected_sibling_call_id": invalid_id,
                "rejected_sibling_tool": invalid_name,
                "rejected_sibling_reason": invalid_reason,
                "message": (
                    f"No call in this batch executed. Sibling call {invalid_id} "
                    f"({invalid_name}) was invalid before execution "
                    f"({invalid_reason}), so the whole batch was rejected "
                    "rather than running an accepted prefix. Re-issue the "
                    "valid calls alone."
                ),
            },
            ensure_ascii=False,
        )
        context.history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                result=payload,
                extras={
                    "batch_rejected": True,
                    "reason": "tool_batch_rejected_before_execution",
                },
            )
        )


def _invalid_call_failure_class(kind: str) -> str:
    return {
        "structure": "tool_call_malformed",
        "parse": "tool_call_arguments_unparsable",
        "schema": "tool_call_schema_violation",
        "exposure": "tool_call_not_exposed",
    }.get(kind, "tool_call_invalid")


def _observe_preflight_failure(guard: Any, invalid: dict[str, Any]) -> None:
    """Open the guard's failure recovery for a non-guard preflight rejection.

    A call rejected as malformed, unparsable, schema-violating, or
    not-exposed is a tool failure the model must recover from by re-reading
    and re-proposing: opening failure recovery keeps the next round's rereads
    unblocked.  Guard and limit rejections deliberately do not open it — they
    are the guard's own brake, and re-opening recovery would let a repeat
    read retry.
    """
    invalid_kind = invalid.get("kind")
    if invalid_kind not in ("structure", "parse", "schema", "exposure"):
        return
    guard.observe_result(
        str(invalid.get("name") or "<unknown>"),
        False,
        json.dumps(
            {
                "ok": False,
                "error": str(invalid.get("error") or "invalid tool call"),
                "failure_class": _invalid_call_failure_class(invalid_kind),
            },
            ensure_ascii=False,
        ),
    )


def _edit_recovery_pending(state: _SendState) -> bool:
    """Return whether existing edit-recovery state requires a fresh reread.

    Reads the send state the loop already keeps — no new bookkeeping.  While
    any of these are outstanding the model has been *told* to re-read a file,
    so the pre-edit loop guard must not block it for doing so.
    """
    return bool(
        state.line_range_reread_required
        or state.edit_fallback_required
        or state.syntax_repair_required
        or state.syntax_validation_required
        or state.patch_invalid_syntax_required
    )


def _result_payload_applied(payload: Any) -> bool:
    """Return whether a write tool's result payload explicitly proves the change landed.

    Fail-closed: an ambiguous result is not an applied write. A write counts
    only when the payload's ``applied`` field is exactly ``True`` — a malformed
    payload, a non-dict payload, or a payload with no ``applied`` field all mean
    "do not treat as applied". The requested tool name and path never count as
    proof; only the result payload does.
    """
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
    if isinstance(data, dict):
        return data.get("applied") is True
    return False


def _enclosing_result_success(res: dict[str, Any]) -> bool | None:
    """Return the enclosing tool result's success, or ``None`` when unknown.

    Write results carry the emitted ``ToolResult`` event and a ``flow_result``,
    both reflecting the tool's authoritative ``ok``. When either is present it
    must say success; ``None`` means the round recorded no such signal (synthetic
    unit-test results), in which case the payload's own explicit ``applied``
    field is the only evidence.
    """
    event = res.get("event")
    if event is not None:
        ok = getattr(event, "ok", None)
        if ok is not None:
            return bool(ok)
    flow = res.get("flow_result")
    if isinstance(flow, dict) and flow.get("ok") is not None:
        return bool(flow["ok"])
    return None


def _applied_write_paths(
    tasks: list[dict[str, Any]],
    results_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Normalized paths of write tools whose result explicitly proved the change applied.

    Legacy dispatch reports modified files through ``planner_stale_read_files``
    on the dispatch result. Production SINGLE writes run directly, so the
    applied paths must come from the write tool's own result — otherwise the
    silent post-write refresh never fires. A path counts only when the result
    payload says ``applied: True`` *and* the enclosing tool result (when the
    round recorded one) also reports success; an ambiguous or failed result
    never counts.
    """
    files: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        name = task["name"]
        if name not in WRITE_TOOLS:
            continue
        res = results_by_id.get(task["id"])
        if res is None:
            continue
        if not _result_payload_applied(res.get("result_payload")):
            continue
        if _enclosing_result_success(res) is False:
            continue
        path = _edit_shapes.tool_path(name, task["args"])
        if not path:
            continue
        normalized = str(path).replace("\\", "/")
        if normalized not in seen:
            files.append(normalized)
            seen.add(normalized)
    return files


def _combined_post_write_files(
    planner_files: list[str], write_files: list[str]
) -> list[str]:
    """Merge dispatch-reported and direct-write file lists, deduplicated."""
    seen: set[str] = set()
    merged: list[str] = []
    for group in (planner_files, write_files):
        for raw in group:
            path = str(raw or "").replace("\\", "/")
            if not path or path in seen:
                continue
            seen.add(path)
            merged.append(path)
    return merged


#: The runtime's edit-recovery owners, mapped to the requirement name a
#: trajectory event reports.  These are the *only* places a concrete recovery
#: requirement is recorded; a failed write that appears in none of them created
#: no requirement, and did not move the implementation.
_RECOVERY_REQUIREMENT_OWNERS: tuple[tuple[str, str], ...] = (
    ("line_range_reread_required", "line_range_reread"),
    ("edit_fallback_required", "edit_fallback"),
    ("syntax_repair_required", "syntax_repair"),
    ("patch_invalid_syntax_required", "patch_invalid_syntax"),
    ("syntax_validation_required", "syntax_validation"),
)

#: Authoritative failure classes that are harness- or user-side refusals rather
#: than edit failures.  None of them can produce implementation movement even if
#: some unrelated recovery owner happened to change in the same round: the user
#: declining an approval, a malformed or unexposed call, a limit rejection, and
#: an internal harness exception all leave the edit exactly where it was.
_NON_RECOVERY_FAILURE_CLASSES: frozenset[str] = frozenset({
    "approval_rejected",
    "internal_tool_error",
    "tool_call_malformed",
    "tool_call_arguments_unparsable",
    "tool_call_schema_violation",
    "tool_call_not_exposed",
    "tool_call_invalid",
    "tool_call_limit",
    "tool_unavailable",
})


def _recovery_requirement_snapshot(state: _SendState) -> dict[str, frozenset[str]]:
    """Normalised paths currently held by each edit-recovery owner.

    Taken before and after the round's calls execute; the difference is the set
    of requirements this round actually created.  Reads the send state the
    runtime already keeps — no new bookkeeping, and no inference from tool names.
    """
    snapshot: dict[str, frozenset[str]] = {}
    for attr, _requirement in _RECOVERY_REQUIREMENT_OWNERS:
        held = getattr(state, attr, None) or ()
        snapshot[attr] = frozenset(
            str(path).replace("\\", "/") for path in held
        )
    return snapshot


def _result_failure_class(res: dict[str, Any]) -> str:
    """The authoritative failure class recorded on one tool result."""
    payload = res.get("result_payload")
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = None
    if isinstance(data, dict):
        failure_class = str(data.get("failure_class") or "").strip()
        if failure_class:
            return failure_class
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip().splitlines()[0][:120]
    return "write_failed"


def _edit_recovery_events(
    tasks: list[dict[str, Any]],
    results_by_id: dict[str, dict[str, Any]],
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
) -> list[EditRecoveryEvent]:
    """Mutation failures that actually created concrete edit-recovery state.

    "Write tool plus not applied" is *not* the test, and inferring recovery from
    it was wrong: an approval the user declined, a rejected or malformed call, an
    internal tool exception, an unavailable tool, a batch rejection, and a
    harness limit rejection are all write results that never applied and never
    created any recovery requirement either.  Counting them as implementation
    movement let a turn reset its trajectory budget by failing in ways that
    changed nothing.

    The authoritative test is a *state delta in the runtime's own recovery
    owners* (:data:`_RECOVERY_REQUIREMENT_OWNERS`).  A path that one of them
    started holding during this round is a real requirement the model must now
    satisfy, and pairing it back to the write whose authoritative failure created
    it yields the three facts an event must name: the path, the requirement, and
    the failure.

    Note what this means today: those owners are populated by
    :func:`~aura.conversation.manager_recovery.update_worker_recovery_state`,
    which the round applies in worker mode only.  Production SINGLE therefore
    records no recovery requirement at all, so no failed write there resets the
    trajectory segment — which is the fail-closed direction, and the correct one.
    A turn fighting a hard edit is not harmed by that: a rollover retires the
    *investigation*, never the mutation blocks or the unresolved write failures,
    so the failed edit and its evidence stay in front of the model.
    """
    created: dict[str, str] = {}
    for attr, requirement in _RECOVERY_REQUIREMENT_OWNERS:
        for path in sorted(after.get(attr, frozenset()) - before.get(attr, frozenset())):
            created.setdefault(path, requirement)
    if not created:
        return []

    events: list[EditRecoveryEvent] = []
    seen: set[str] = set()
    for task in tasks:
        name = task["name"]
        if name not in WRITE_TOOLS:
            continue
        res = results_by_id.get(task["id"])
        if res is None:
            continue
        applied = (
            _result_payload_applied(res.get("result_payload"))
            and _enclosing_result_success(res) is not False
        )
        if applied:
            continue
        failure_class = _result_failure_class(res)
        if failure_class in _NON_RECOVERY_FAILURE_CLASSES:
            continue
        raw = _edit_shapes.tool_path(name, task["args"])
        if not raw:
            continue
        path = str(raw).replace("\\", "/")
        requirement = created.get(path)
        if requirement is None or path in seen:
            continue
        seen.add(path)
        events.append(
            EditRecoveryEvent(
                path=path,
                requirement=requirement,
                failure_class=failure_class,
            )
        )
    return events


def _trajectory_facts(
    *,
    tasks: list[dict[str, Any]],
    results_by_id: dict[str, dict[str, Any]],
    applied_write_paths: list[str],
    stale_paths: list[str],
    recovery_before: dict[str, frozenset[str]],
    recovery_after: dict[str, frozenset[str]],
    blocker_succeeded: bool,
    already_satisfied_succeeded: bool,
) -> RoundFacts:
    """Project one completed round into the trajectory owner's facts.

    ``observation_tokens`` is the actual size of newly accepted observation
    results, in the same ``len // 4`` unit the context ladder budgets in.  It is
    **telemetry**: the segment is spent against repeated model-request cost,
    which the send loop charges directly, because a hundred tiny reads are
    expensive in requests and cheap in payload bytes.

    A failed observation contributes nothing — no tokens, and no survey
    fingerprint, so recovering from a failure is never treated as a resurvey.
    Neither does a call that never executed, because a rejected batch returns
    before this runs.
    """
    observation_tokens = 0
    targets: list[str] = []
    fingerprints: list[str] = []
    command_executed = False
    for task in tasks:
        res = results_by_id.get(task["id"])
        if res is None:
            continue
        if task["effect"] is ToolEffect.COMMAND:
            command_executed = True
            continue
        if task["effect"] is not ToolEffect.OBSERVATION:
            continue
        if _enclosing_result_success(res) is False:
            continue
        payload = res.get("result_payload")
        if isinstance(payload, str):
            observation_tokens += len(payload) // 4
        for target in observation_targets(task["name"], task["args"]):
            if target not in targets:
                targets.append(target)
        if not is_narrow_read(task["name"], task["args"]):
            fingerprints.append(read_fingerprint(task["name"], task["args"]))

    return RoundFacts(
        observation_tokens=observation_tokens,
        observation_targets=tuple(targets),
        broad_observation_fingerprints=tuple(fingerprints),
        applied_write_paths=tuple(applied_write_paths),
        stale_paths=tuple(stale_paths),
        edit_recovery_events=tuple(
            _edit_recovery_events(
                tasks, results_by_id, recovery_before, recovery_after
            )
        ),
        command_executed=command_executed,
        blocker_succeeded=blocker_succeeded,
        already_satisfied_succeeded=already_satisfied_succeeded,
    )


@dataclass(frozen=True)
class ToolRoundOutcome:
    action: str
    enter_silent_preflight: bool = False
    blocker_succeeded: bool = False
    already_satisfied_succeeded: bool = False
    #: Authoritative trajectory facts for
    #: :class:`~aura.conversation.single_trajectory.SingleTrajectoryController`.
    #: ``None`` when the round ended before any result was authoritative
    #: (cancellation, a wholly rejected batch), in which case nothing is counted.
    trajectory_facts: RoundFacts | None = None


class ToolRoundRunner:
    """Execute one assistant tool-call round and apply resulting state."""

    def __init__(
        self,
        *,
        history: History,
        tools: ToolRegistry,
        tool_runner: ToolRunner,
        planner_refresh: PlannerRefreshState,
        lifecycle: LifecycleHooks | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._history = history
        self._tools = tools
        self._tool_runner = tool_runner
        self._planner_refresh = planner_refresh
        self._lifecycle = lifecycle
        self._event_bus = event_bus

    def run(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        state: _SendState,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        cleanup_cancelled: Callable[[EventCallback], None],
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
        declared_run_command: str | None = None,
        tool_defs: list[dict[str, Any]] | None = None,
    ) -> ToolRoundOutcome:
        terminal_dispatch = False
        worker_phase_boundary_info: dict[str, Any] | None = None
        enter_silent_preflight = False

        # The exact tool surface the request that produced these calls offered.
        # Callers that know the request's own catalog (focused action, blocked
        # turn) pass it; otherwise the registry's current catalog is the best
        # approximation of what was exposed.
        if tool_defs is None:
            tool_defs = self._tools.tool_defs()

        guard = state.pre_edit_guard
        if guard is not None:
            guard.begin_round()
        recovery_pending = _edit_recovery_pending(state)

        # ── Batch preflight ──────────────────────────────────────────────
        # Every proposed call is structurally validated, checked against the
        # tools actually exposed in this request, JSON-schema validated,
        # classified against the registry's authoritative tool-effect
        # metadata, and checked by the limit and loop-guard gates *before any
        # call executes*.  If any call is invalid the whole batch is rejected
        # coherently: no accepted prefix runs, and every rejected tool call
        # receives exactly one paired tool result.
        invalid: dict[str, Any] | None = preflight_structural(tool_calls)
        if invalid is not None:
            _reject_tool_call_batch(
                tool_calls=tool_calls,
                invalid=invalid,
                context=ToolRoundEventsContext(history=self._history),
                on_event=on_event,
            )
            if guard is not None:
                _observe_preflight_failure(guard, invalid)
                guard.end_round()
            return ToolRoundOutcome(action="next_round")

        # The callable surface is the exposed catalog plus the observation-only
        # names this mode withholds but still honours on replay. Anything else
        # is not callable, so a withheld mutation stays withheld.
        exposed = exposed_tool_schemas(tool_defs)
        for name, schema in exposed_tool_schemas(self._tools.replayable_tool_defs()).items():
            exposed.setdefault(name, schema)
        preflighted: list[dict[str, Any]] = []
        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            tool_call_id = tc["id"]
            args, parse_error = decode_arguments(fn.get("arguments"))
            if parse_error is not None:
                invalid = {
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "kind": "parse",
                    "error": parse_error,
                }
                break

            if name not in exposed:
                invalid = {
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "kind": "exposure",
                    "error": (
                        f"tool '{name}' is not exposed in this request; only the "
                        "exposed tool catalog is callable"
                    ),
                }
                break

            schema_violations = schema_errors(name, args, exposed[name])
            if schema_violations:
                invalid = {
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "kind": "schema",
                    "error": "; ".join(schema_violations),
                    "failure_class": "tool_call_schema_violation",
                }
                break

            effect = self._tools.tool_effect(name)
            allowed, limit_info = state.limits.check(name)
            if not allowed:
                invalid = {
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "kind": "limit",
                    "limit_info": limit_info,
                }
                if is_recoverable_phase_boundary(limit_info):
                    worker_phase_boundary_info = limit_info
                break

            if guard is not None:
                repeat_info = guard.check(
                    name, args, recovery_pending=recovery_pending
                )
                if repeat_info is not None:
                    invalid = {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "kind": "guard",
                        "repeat_info": repeat_info,
                    }
                    break

            preflighted.append(
                {"id": tool_call_id, "name": name, "args": args, "effect": effect}
            )

        if invalid is not None:
            _reject_tool_call_batch(
                tool_calls=tool_calls,
                invalid=invalid,
                context=ToolRoundEventsContext(history=self._history),
                on_event=on_event,
            )
            if guard is not None:
                _observe_preflight_failure(guard, invalid)
                guard.end_round()
            if worker_phase_boundary_info is not None:
                if worker_phase_boundary_info.get("message"):
                    self._history.append_user_text(
                        str(worker_phase_boundary_info["message"])
                    )
                return ToolRoundOutcome(action="continue")
            return ToolRoundOutcome(action="next_round")

        tasks = preflighted
        for task in tasks:
            state.limits.record(task["name"])
            if guard is not None:
                # The call id is what the outbound API view keys result
                # residency on, so the guard can tell a duplicate read whose
                # result is still in front of the model from one whose result
                # compaction has already retired.
                guard.record(task["name"], task["args"], task["id"])
            if state.worker_flow is not None:
                state.worker_flow.observe_tool_call(task["name"], task["args"])

        if cancel_event.is_set():
            cleanup_cancelled(on_event)
            return ToolRoundOutcome(action="return")

        def process_task(task: dict[str, Any]) -> dict[str, Any]:
            nonlocal terminal_dispatch, worker_phase_boundary_info
            try:
                result = self._process_task(
                    task=task,
                    state=state,
                    on_event=on_event,
                    approval_cb=approval_cb,
                    cancel_event=cancel_event,
                    dispatch_cb=dispatch_cb,
                    workflow_state_cb=workflow_state_cb,
                    explicit_validation_commands=explicit_validation_commands,
                    declared_run_command=declared_run_command,
                )
            except Exception as exc:
                # Last-resort containment: an unexpected exception from any
                # task processor (executor, dispatch, terminal) becomes one
                # redacted internal error result instead of crashing the
                # round or the worker.
                from aura.config import redact_secrets

                redacted = redact_secrets(f"{type(exc).__name__}: {exc}")
                payload = {
                    "ok": False,
                    "error": "internal tool error — the harness could not run this call",
                    "failure_class": "internal_tool_error",
                    "internal_error": redacted,
                    "tool": task["name"],
                }
                payload_json = json.dumps(payload, ensure_ascii=False)
                return {
                    "id": task["id"],
                    "result_payload": payload_json,
                    "event": ToolResult(
                        tool_call_id=task["id"],
                        name=task["name"],
                        ok=False,
                        result=payload_json,
                        extras={"internal_tool_error": True},
                    ),
                    "flow_result": {
                        "name": task["name"],
                        "args": task["args"],
                        "ok": False,
                        "result_payload": payload_json,
                    },
                }
            if result.pop("terminal_dispatch", False):
                terminal_dispatch = True
            phase_boundary = result.pop("_worker_phase_boundary_info", None)
            if is_recoverable_phase_boundary(phase_boundary):
                worker_phase_boundary_info = phase_boundary
            return result

        # What the runtime's edit-recovery owners held before any call ran. The
        # difference after the round is the authoritative record of which failed
        # writes actually created a concrete recovery requirement — the only
        # kind of failed write that counts as implementation movement.
        recovery_before = _recovery_requirement_snapshot(state)

        results_to_append: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures: dict[concurrent.futures.Future[dict[str, Any]], dict[str, Any]] = {}
            for task in tasks:
                if cancel_event.is_set():
                    break

                if task["effect"] is ToolEffect.OBSERVATION:
                    futures[executor.submit(process_task, task)] = task
                else:
                    for fut in concurrent.futures.as_completed(futures):
                        results_to_append.append(fut.result())
                    futures.clear()

                    if cancel_event.is_set():
                        break

                    results_to_append.append(process_task(task))

            for fut in concurrent.futures.as_completed(futures):
                results_to_append.append(fut.result())

        results_by_id = {r.get("id"): r for r in results_to_append if r is not None}

        # Direct write tools (production SINGLE) report their applied paths in
        # the result; legacy dispatch reports them via planner_stale_read_files.
        applied_write_paths = _applied_write_paths(tasks, results_by_id)

        # Whether a ``report_blocker`` call in this round actually succeeded
        # with the structured blocker payload. The manager's blocker
        # finalization must be terminal only on this fact, never on the tool
        # name alone.
        blocker_succeeded = False
        for task in tasks:
            if task["name"] != "report_blocker":
                continue
            res = results_by_id.get(task["id"])
            if not res:
                continue
            payload = parse_tool_payload(str(res.get("result_payload", "")))
            if (
                bool(payload.get("ok"))
                and bool(payload.get("blocker_reported"))
                and payload.get("mutation") is False
                and payload.get("applied") is False
            ):
                blocker_succeeded = True
            break

        # Whether a ``report_already_satisfied`` call in this round actually
        # succeeded with the structured payload. Completion as already-satisfied
        # is terminal only on this fact — never on the tool name alone, never
        # on the absence of a write, and never on assistant prose.
        already_satisfied_succeeded = False
        for task in tasks:
            if task["name"] != REPORT_ALREADY_SATISFIED:
                continue
            res = results_by_id.get(task["id"])
            if not res:
                continue
            payload = parse_tool_payload(str(res.get("result_payload", "")))
            if (
                bool(payload.get("ok"))
                and bool(payload.get("already_satisfied_reported"))
                and payload.get("mutation") is False
                and payload.get("applied") is False
            ):
                already_satisfied_succeeded = True
            break

        completed_dispatch_for_final = False
        completed_tool_result_for_final = False
        planner_stale_read_files: list[str] = []
        for task in tasks:
            if cancel_event.is_set():
                cleanup_cancelled(on_event)
                return ToolRoundOutcome(action="return")

            res = results_by_id.get(task["id"])
            if not res:
                continue

            planner_stale_read_files.extend(
                str(path) for path in res.get("planner_stale_read_files", [])
            )
            if res.get("blocker"):
                self._planner_refresh.handle_post_write_notices(
                    self._history,
                    _combined_post_write_files(
                        planner_stale_read_files, applied_write_paths
                    ),
                )
                blocker_reason = str(res.get("blocker_reason", ""))
                failure_constraint = res.get("failure_constraint", "")

                append_dispatch_blocker_message(
                    context=ToolRoundEventsContext(history=self._history),
                    result=res["result"],
                    reason=blocker_reason,
                    on_event=on_event,
                    failure_constraint=failure_constraint,
                    attempt_brief=res.get("attempt_brief"),
                )
                return ToolRoundOutcome(action="return")
            if res.get("completed_dispatch_for_final"):
                completed_dispatch_for_final = True
            if res.get("completed_tool_result_for_final"):
                completed_tool_result_for_final = True
            if res.get("enter_silent_preflight"):
                enter_silent_preflight = True
            if res.get("flow_result"):
                flow_result = res["flow_result"]
                if state.worker_flow is not None:
                    state.worker_flow.observe_tool_result(
                        flow_result.get("name", task["name"]),
                        flow_result.get("args", task["args"]),
                        flow_result.get("ok"),
                        flow_result.get("result_payload"),
                    )
                if guard is not None:
                    guard.observe_result(
                        str(flow_result.get("name", task["name"])),
                        bool(flow_result.get("ok")),
                        flow_result.get("result_payload"),
                    )
            elif guard is not None and "result_payload" in res:
                event = res.get("event")
                guard.observe_result(
                    task["name"],
                    bool(getattr(event, "ok", True)),
                    res.get("result_payload"),
                )
            planner_constraint = str(res.get("planner_internal_constraint", "") or "")
            attempt_brief = res.get("attempt_brief")
            if attempt_brief is not None:
                self._history.append_internal_user_text(
                    render_for_planner(attempt_brief)
                )
            elif planner_constraint:
                self._history.append_internal_user_text(planner_constraint)
            if res.get("skip"):
                continue

            if "result_payload" in res:
                self._history.append_tool_result(task["id"], res["result_payload"])
                on_event(res["event"])

        self._planner_refresh.handle_post_write_notices(
            self._history,
            _combined_post_write_files(planner_stale_read_files, applied_write_paths),
        )

        # Paths a write or a stale-file notice changed this round. The duplicate
        # guard forgets its fingerprints for them, and so does the trajectory
        # owner's retained-survey set: bytes that changed are always rereadable.
        stale_paths = _combined_post_write_files(
            planner_stale_read_files, applied_write_paths
        )
        recovery_after = _recovery_requirement_snapshot(state)

        if guard is not None:
            # A stale-file notice makes the named paths worth reading again.
            guard.note_stale_paths(stale_paths)
            guard.end_round()

        if worker_phase_boundary_info is not None:
            if worker_phase_boundary_info.get("message"):
                self._history.append_user_text(str(worker_phase_boundary_info["message"]))
            return ToolRoundOutcome(action="continue")

        if completed_dispatch_for_final:
            return ToolRoundOutcome(action="return")
        if completed_tool_result_for_final:
            # The route's requested production action was itself the observation
            # or command that just succeeded. That is implementation progress by
            # definition — the work is done — and the turn moves to its final
            # response rather than to any trajectory judgement.
            state.task_completion_context = True
            return ToolRoundOutcome(
                action="continue",
                trajectory_facts=_trajectory_facts(
                    tasks=tasks,
                    results_by_id=results_by_id,
                    applied_write_paths=applied_write_paths,
                    stale_paths=stale_paths,
                    recovery_before=recovery_before,
                    recovery_after=recovery_after,
                    blocker_succeeded=blocker_succeeded,
                    already_satisfied_succeeded=already_satisfied_succeeded,
                ),
            )

        if terminal_dispatch:
            return ToolRoundOutcome(action="return")
        if enter_silent_preflight:
            return ToolRoundOutcome(action="continue", enter_silent_preflight=True)

        return ToolRoundOutcome(
            action="next_round",
            enter_silent_preflight=enter_silent_preflight,
            blocker_succeeded=blocker_succeeded,
            already_satisfied_succeeded=already_satisfied_succeeded,
            trajectory_facts=_trajectory_facts(
                tasks=tasks,
                results_by_id=results_by_id,
                applied_write_paths=applied_write_paths,
                stale_paths=stale_paths,
                recovery_before=recovery_before,
                recovery_after=recovery_after,
                blocker_succeeded=blocker_succeeded,
                already_satisfied_succeeded=already_satisfied_succeeded,
            ),
        )



    def _process_task(
        self,
        *,
        task: dict[str, Any],
        state: _SendState,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        explicit_validation_commands: list[ValidationCommandSpec] | None,
        declared_run_command: str | None,
    ) -> dict[str, Any]:
        tool_call_id = task["id"]
        name = task["name"]
        args = task["args"]

        if state.mode == "worker":
            blocked = worker_recovery_block(
                self._tools.workspace_root,
                tool_call_id=tool_call_id,
                name=name,
                args=args,
                edit_failed_shapes=state.edit_failed_shapes,
                edit_fallback_required=state.edit_fallback_required,
                recovery_block_counts=state.recovery_block_counts,
                line_range_reread_required=state.line_range_reread_required,
                worker_file_state=state.worker_file_state,
                patch_failed_cycles=state.patch_failed_cycles,
                patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                edit_retry_ledger=state.edit_retry_ledger,
                syntax_repair_required=state.syntax_repair_required,
                syntax_validation_required=state.syntax_validation_required,
                write_attempts_by_path=state.write_attempts_by_path,
            )
            if blocked is not None:
                blocked_payload = parse_tool_payload(str(blocked.get("result_payload", "")))
                if is_recoverable_phase_boundary(blocked_payload):
                    blocked["_worker_phase_boundary_info"] = blocked_payload
                return blocked

        # ── Lifecycle gate: worker.pre_tool_use ─────────────────────────
        if state.mode == "worker" and self._lifecycle is not None:
            gate_result = run_worker_pre_tool_gate(
                context=WorkerPreToolGateContext(
                    history=self._history,
                    tools=self._tools,
                    lifecycle=self._lifecycle,
                    event_bus=self._event_bus,
                ),
                tool_call_id=tool_call_id,
                name=name,
                args=args,
                state=state,
            )
            if gate_result is not None:
                if gate_result.get("blocked"):
                    return blocked_tool_result(
                        tool_call_id,
                        name,
                        gate_result["blocked_payload"],
                    )
                if "rewritten_args" in gate_result:
                    args = gate_result["rewritten_args"]
                    task = dict(task, args=args)

        if name == "dispatch_to_worker":
            return handle_dispatch_to_worker_round(
                context=DispatchToolRoundContext(
                    history=self._history,
                    tool_runner=self._tool_runner,
                ),
                tool_call_id=tool_call_id,
                args=args,
                state=state,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                on_event=on_event,
            )

        if name == "run_and_watch":
            return handle_run_and_watch_round(
                tool_call_id=tool_call_id,
                args=args,
                state=state,
                tool_runner=self._tool_runner,
                on_event=on_event,
                cancel_event=cancel_event,
                declared_run_command=declared_run_command or "",
            )

        if name == "run_terminal_command":
            return handle_run_terminal_command_round(
                tool_call_id=tool_call_id,
                args=args,
                state=state,
                tool_runner=self._tool_runner,
                workspace_root=Path(self._tools.workspace_root),
                on_event=on_event,
                cancel_event=cancel_event,
                explicit_validation_commands=explicit_validation_commands,
            )

        if state.reject_all_for_turn and name in WRITE_TOOLS:
            payload = json.dumps(
                {
                    "ok": False,
                    "error": "User rejected all writes in this turn.",
                    "failure_class": "approval_rejected",
                    "applied": False,
                    "write_outcome": "not_applied_user_rejected",
                }
            )
            return {
                "id": tool_call_id,
                "result_payload": payload,
                "event": ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    result=payload,
                    extras={"approval": "reject_all"},
                ),
                "flow_result": {
                    "name": name,
                    "args": args,
                    "ok": False,
                    "result_payload": payload,
                },
            }

        try:
            exec_result = self._tools.execute(
                name=name,
                args=args,
                approval_cb=approval_cb,
                reject_all=False,
                cancel_event=cancel_event,
                skill_turn_state=getattr(state, "skill_turn", None),
            )
        except Exception as exc:
            # A handler bug must never escape the tool round or crash the
            # worker: it becomes a redacted internal error result for this one
            # call.  Only the exception type is exposed to the model; the
            # message is redacted of known secrets.
            from aura.config import redact_secrets

            redacted = redact_secrets(f"{type(exc).__name__}: {exc}")
            payload = {
                "ok": False,
                "error": "internal tool error — the harness could not run this call",
                "failure_class": "internal_tool_error",
                "internal_error": redacted,
                "tool": name,
            }
            payload_json = json.dumps(payload, ensure_ascii=False)
            return {
                "id": tool_call_id,
                "result_payload": payload_json,
                "event": ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    result=payload_json,
                    extras={"internal_tool_error": True},
                ),
                "flow_result": {
                    "name": name,
                    "args": args,
                    "ok": False,
                    "result_payload": payload_json,
                },
            }

        if exec_result.extras.get("approval") == "reject_all":
            state.reject_all_for_turn = True

        tool_msg_content = exec_result.to_tool_message_content()
        if state.mode == "worker":
            tool_msg_content = update_worker_recovery_state(
                self._tools.workspace_root,
                name=name,
                args=args,
                ok=exec_result.ok,
                content=tool_msg_content,
                edit_failed_shapes=state.edit_failed_shapes,
                edit_fallback_required=state.edit_fallback_required,
                line_range_reread_required=state.line_range_reread_required,
                worker_file_state=state.worker_file_state,
                patch_failed_cycles=state.patch_failed_cycles,
                patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                edit_retry_ledger=state.edit_retry_ledger,
                syntax_repair_required=state.syntax_repair_required,
                syntax_validation_required=state.syntax_validation_required,
                write_attempts_by_path=state.write_attempts_by_path,
                worker_app_writes=state.worker_app_writes,
            )

        result = {
            "id": tool_call_id,
            "result_payload": tool_msg_content,
            "event": ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=exec_result.ok,
                result=tool_msg_content,
                extras=exec_result.extras,
            ),
            "completed_tool_result_for_final": (
                state.mode in {"planner", "single"}
                and tool_result_completes_action(
                    name,
                    exec_result.ok,
                    probes_complete_action=state.probes_complete_action(),
                )
            ),
            "flow_result": {
                "name": name,
                "args": args,
                "ok": exec_result.ok,
                "result_payload": tool_msg_content,
            },
        }
        if state.mode == "planner" and exec_result.extras.get("planner_tool_unavailable"):
            result["planner_internal_constraint"] = str(
                exec_result.extras.get("failure_constraint", "") or ""
            )
            result["completed_tool_result_for_final"] = False
        return result



__all__ = ["ToolRoundOutcome", "ToolRoundRunner"]
