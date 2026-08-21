"""Tool-call round execution for ConversationManager.

One round: structurally validate the whole proposed batch, execute it, and
append exactly one truthful result per call in original call order. Validation
is structural only — tool-call shape, JSON arguments, an exposed tool name, and
JSON-schema conformance. Nothing here counts calls, caps them, rejects a repeat,
or decides whether the turn may end.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from aura.client import Event, ToolResult
from aura.conversation.history import History
from aura.conversation.plan_review import blocked_tool_payload
from aura.conversation.tool_names import WRITE_TOOLS
from aura.conversation.tool_preflight import (
    decode_arguments,
    exposed_tool_schemas,
    preflight_structural,
    schema_errors,
)
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalCallback
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.tools.file_edit_lifecycle import (
    FILE_EDIT_LIFECYCLE_TOOLS,
    FileEditLifecycleTracker,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.validation_orchestrator import ValidationCommandSpec
from aura.events import EventBus
from aura.skills.turn_state import SkillTurnState

EventCallback = Callable[[Event], None]


def _preflight_failure_payload(
    *,
    tool_call_id: str,
    name: str,
    error: str,
    failure_class: str,
    message: str,
) -> tuple[str, ToolResult]:
    """Build the one shared shape for a preflight-rejected call's own result.

    Returns ``(payload_json, event)``, used both for the invalid call's own
    entry in a whole-batch rejection and for a call rejected on its own in an
    effect-aware partial batch — so the two call sites never diverge.
    """
    extras: dict[str, Any] = {
        "call_rejected": True,
        "reason": "tool_call_invalid_before_execution",
        "failure_class": failure_class,
    }
    payload = json.dumps(
        {
            "ok": False,
            "error": error,
            "recoverable": True,
            "failure_class": failure_class,
            "tool": name,
            "call_rejected": True,
            "reason": "tool_call_invalid_before_execution",
            "message": message,
        },
        ensure_ascii=False,
    )
    event = ToolResult(
        tool_call_id=tool_call_id,
        name=name,
        ok=False,
        result=payload,
        extras=extras,
    )
    return payload, event


def _isolated_schema_failure_result(tool_call_id: str, name: str, error: str) -> dict[str, Any]:
    """The per-call result for a schema-invalid call in an isolatable batch.

    Unlike whole-batch rejection, this call's valid siblings still execute —
    only known ``OBSERVATION``/``BOOKKEEPING`` tools ever reach this path (see
    ``ToolRoundRunner.run``), so this call's own failure carries no risk to
    the rest of the batch.
    """
    payload, event = _preflight_failure_payload(
        tool_call_id=tool_call_id,
        name=name,
        error=error,
        failure_class="tool_call_schema_violation",
        message=(
            f"Call {tool_call_id} ({name}) was invalid before execution ({error}) and "
            "was rejected on its own. This batch contained only observation/bookkeeping "
            "calls, so valid sibling calls still executed. Re-issue the corrected call."
        ),
    )
    return {"id": tool_call_id, "result_payload": payload, "event": event}


def _reject_tool_call_batch(
    *,
    tool_calls: list[dict[str, Any]],
    invalid: dict[str, Any],
    history: History,
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
    invalid_error = str(invalid.get("error") or "invalid tool call")
    invalid_reason = invalid_error
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
            payload, event = _preflight_failure_payload(
                tool_call_id=tool_call_id,
                name=name,
                error=invalid_error,
                failure_class=failure_class,
                message=(
                    f"No call in this batch executed. Call {tool_call_id} "
                    f"({name}) was invalid before execution "
                    f"({invalid_reason}), so the whole batch was rejected. "
                    "Re-issue the corrected call(s)."
                ),
            )
            history.append_tool_result(tool_call_id, payload)
            on_event(event)
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
        history.append_tool_result(tool_call_id, payload)
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


@dataclass(frozen=True)
class ToolRoundOutcome:
    """What the manager needs to know after one executed batch.

    ``cancelled`` is the only field that affects the loop, and only because the
    user stopped the run.
    """

    cancelled: bool = False


class ToolRoundRunner:
    """Execute one assistant tool-call round and append its results."""

    def __init__(
        self,
        *,
        history: History,
        tools: ToolRegistry,
        tool_runner: ToolRunner,
        event_bus: EventBus | None = None,
    ) -> None:
        self._history = history
        self._tools = tools
        self._tool_runner = tool_runner
        self._event_bus = event_bus
        # Approval bookkeeping: the user answered "reject all writes" for the
        # current turn. Not policy — it is the user's own decision, relayed.
        self._reject_all_for_turn = False

    def begin_turn(self) -> None:
        """Clear per-turn approval bookkeeping at the start of a send."""
        self._reject_all_for_turn = False

    def run(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        cleanup_cancelled: Callable[[EventCallback], None],
        skill_turn: SkillTurnState | None = None,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
        tool_defs: list[dict[str, Any]] | None = None,
    ) -> ToolRoundOutcome:
        # The exact tool surface the request that produced these calls offered.
        if tool_defs is None:
            tool_defs = self._tools.tool_defs()

        # ── Batch preflight ──────────────────────────────────────────────
        # Every proposed call is structurally validated, checked against the
        # tools actually exposed in this request, and JSON-schema validated
        # *before any call executes*.  If any call is invalid the whole batch is
        # rejected coherently: no accepted prefix runs, and every rejected tool
        # call receives exactly one paired tool result.
        invalid: dict[str, Any] | None = preflight_structural(tool_calls)
        if invalid is not None:
            _reject_tool_call_batch(
                tool_calls=tool_calls,
                invalid=invalid,
                history=self._history,
                on_event=on_event,
            )
            return ToolRoundOutcome()

        # The callable surface is exactly the tool catalog sent with this
        # request. A tool call executes only if its name and schema were
        # present in that exact request; a registered-but-withheld tool
        # (e.g. inspect_code, search_codebase, find_usages, git_log) is not
        # callable merely because its handler still exists.
        exposed = exposed_tool_schemas(tool_defs)

        # Pass 1: decode arguments and resolve exposure + effect for every
        # call. A parse failure or an unexposed name vetoes the whole batch —
        # unlike a schema violation there is no per-call name/effect here to
        # reason about safely, so these stay all-or-nothing.
        decoded_calls: list[dict[str, Any]] = []
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

            decoded_calls.append(
                {
                    "id": tool_call_id,
                    "name": name,
                    "args": args,
                    "effect": self._tools.tool_effect(name),
                }
            )

        if invalid is not None:
            _reject_tool_call_batch(
                tool_calls=tool_calls,
                invalid=invalid,
                history=self._history,
                on_event=on_event,
            )
            return ToolRoundOutcome()

        # Pass 2: schema-validate each call. A batch may isolate a
        # schema-invalid call as its own failure — valid siblings still
        # execute — only when *every* call in it is a known OBSERVATION or
        # BOOKKEEPING effect (the authoritative lookup from pass 1, never a
        # name-prefix guess). Anything that could touch the workspace or run
        # a command (MUTATION/COMMAND, or an unrecognised extensible tool
        # defaulting to COMMAND) keeps the whole batch all-or-nothing, so a
        # schema failure there still vetoes every sibling, including
        # ``apply_patch``/``shell``.
        safe_effects = {ToolEffect.OBSERVATION, ToolEffect.BOOKKEEPING}
        schema_isolatable = all(dc["effect"] in safe_effects for dc in decoded_calls)

        # The full ordered call list: executable tasks keep {id, name, args,
        # effect}; a call isolated on its own schema failure is instead a
        # precomputed {id, result_payload, event} result. Original call order
        # is preserved either way, so exactly one result per call comes out
        # in original order regardless of which path each call took.
        preflighted: list[dict[str, Any]] = []
        for dc in decoded_calls:
            schema_violations = schema_errors(dc["name"], dc["args"], exposed[dc["name"]])
            if schema_violations:
                error = "; ".join(schema_violations)
                if schema_isolatable:
                    preflighted.append(
                        _isolated_schema_failure_result(dc["id"], dc["name"], error)
                    )
                    continue
                invalid = {
                    "tool_call_id": dc["id"],
                    "name": dc["name"],
                    "kind": "schema",
                    "error": error,
                    "failure_class": "tool_call_schema_violation",
                }
                break
            preflighted.append(dc)

        if invalid is not None:
            _reject_tool_call_batch(
                tool_calls=tool_calls,
                invalid=invalid,
                history=self._history,
                on_event=on_event,
            )
            return ToolRoundOutcome()

        tasks = [t for t in preflighted if "args" in t]

        if cancel_event.is_set():
            cleanup_cancelled(on_event)
            return ToolRoundOutcome(cancelled=True)

        def process_task(task: dict[str, Any]) -> dict[str, Any]:
            try:
                result = self._process_task(
                    task=task,
                    on_event=on_event,
                    approval_cb=approval_cb,
                    cancel_event=cancel_event,
                    skill_turn=skill_turn,
                    explicit_validation_commands=explicit_validation_commands,
                )
            except Exception as exc:
                # Last-resort containment: an unexpected exception from any
                # task processor (executor, terminal) becomes one redacted
                # internal error result instead of crashing the round or the
                # execution.
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
                }
            return result

        # Seed with calls already resolved in preflight (isolated schema
        # failures) — they never reach process_task, but still need exactly
        # one result appended in original call order below.
        results_to_append: list[dict[str, Any]] = [t for t in preflighted if "args" not in t]
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

        # Exactly one result per call, in original call order. Iterates the
        # full preflighted list (executable tasks and isolated schema
        # failures alike) so original call order survives either path.
        for task in preflighted:
            if cancel_event.is_set():
                cleanup_cancelled(on_event)
                return ToolRoundOutcome(cancelled=True)

            res = results_by_id.get(task["id"])
            if not res:
                continue
            if res.get("skip"):
                # The handler already appended its own authoritative result
                # (terminal tools stream their output as they run).
                continue
            if "result_payload" in res:
                self._history.append_tool_result(task["id"], res["result_payload"])
                on_event(res["event"])

        return ToolRoundOutcome()

    def _process_task(
        self,
        *,
        task: dict[str, Any],
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        skill_turn: SkillTurnState | None,
        explicit_validation_commands: list[ValidationCommandSpec] | None,
    ) -> dict[str, Any]:
        tool_call_id = task["id"]
        name = task["name"]
        args = task["args"]

        # Plan Review's runtime guarantee applies here first because ``shell``
        # below is special-cased straight to ToolRunner and never reaches
        # ToolRegistry.execute(), which carries the same check for every
        # other tool. Both call sites defer to ``PlanReviewState.blocks`` —
        # the one authoritative policy — so nothing here re-derives which
        # effects are blocked.
        if self._tools.plan_review.blocks(task["effect"]):
            payload = json.dumps(blocked_tool_payload(), ensure_ascii=False)
            return {
                "id": tool_call_id,
                "result_payload": payload,
                "event": ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    result=payload,
                    extras={"failure_class": "plan_review_required"},
                ),
            }

        if name == "shell":
            # Streams its output and appends its own authoritative result.
            self._tool_runner.handle_terminal_command(
                tool_call_id=tool_call_id,
                args=args,
                on_event=on_event,
                cancel_event=cancel_event,
                explicit_validation_commands=explicit_validation_commands,
            )
            return {"id": tool_call_id, "skip": True}

        if self._reject_all_for_turn and name in WRITE_TOOLS:
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
            }

        lifecycle_tracker: FileEditLifecycleTracker | None = None
        effective_approval_cb = approval_cb
        if name in FILE_EDIT_LIFECYCLE_TOOLS:
            lifecycle_tracker = FileEditLifecycleTracker(
                tool_call_id=tool_call_id,
                tool_name=name,
                operation=str(args.get("operation") or ""),
                on_event=on_event,
            )
            effective_approval_cb = lifecycle_tracker.wrap_approval_cb(approval_cb)

        try:
            exec_result = self._tools.execute(
                name=name,
                args=args,
                approval_cb=effective_approval_cb,
                reject_all=False,
                cancel_event=cancel_event,
                skill_turn_state=skill_turn,
            )
        except Exception as exc:
            # A handler bug must never escape the tool round or crash the
            # execution: it becomes a redacted internal error result for this one
            # call.  Only the exception type is exposed to the model; the
            # message is redacted of known secrets.
            from aura.config import redact_secrets

            redacted = redact_secrets(f"{type(exc).__name__}: {exc}")
            if lifecycle_tracker is not None:
                lifecycle_tracker.emit_outcome_failed(redacted)
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
            }

        if exec_result.extras.get("approval") == "reject_all":
            self._reject_all_for_turn = True

        if lifecycle_tracker is not None:
            lifecycle_tracker.emit_outcome(exec_result)

        tool_msg_content = exec_result.to_tool_message_content()

        return {
            "id": tool_call_id,
            "result_payload": tool_msg_content,
            "event": ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=exec_result.ok,
                result=tool_msg_content,
                extras=exec_result.extras,
            ),
        }


__all__ = ["ToolRoundOutcome", "ToolRoundRunner"]
