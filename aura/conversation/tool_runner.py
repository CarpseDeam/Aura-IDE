"""ToolRunner - delegates tool execution for ConversationManager."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from aura.client import (
    TerminalOutput,
    ToolCallStart,
    ToolResult,
    WorkerDispatchRequested,
)
from aura.config import load_settings, redact_secrets
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.dispatch_contract import enrich_worker_dispatch_contract
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.terminal_policy import worker_terminal_command_allowed
from aura.conversation.tool_runner_terminal_policy import (
    matches_explicit_validation,
    resolve_terminal_timeout,
)
from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    VALIDATION_COMMAND_UNRUNNABLE,
    classify_command_outcome,
    classify_terminal_run,
    classify_validation_run,
    looks_like_validation_command,
    parse_validation_command,
)
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.project_env import (
    build_project_command,
    build_project_command_rewrite,
    project_environment_missing_payload,
    resolve_workspace_cwd,
    workspace_relative_cwd,
)
from aura.sandbox import SandboxExecutor, SandboxResult, WatchResult

_log = logging.getLogger(__name__)


class ToolRunner:
    """Owns execution of dispatch and terminal tools."""

    def __init__(
        self,
        history: History,
        workspace_root: Path,
        loop_detector: LoopDetector,
        verification_tracker: VerificationProgressTracker,
    ) -> None:
        self._history = history
        self._workspace_root = workspace_root
        self._loop_detector = loop_detector
        self._verification_tracker = verification_tracker

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def handle_dispatch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        dispatch_cb: DispatchCallback | None,
        workflow_state_cb: Callable[[str, str, str, Any], None] | None = None,
        planner_dispatch_attempt: int = 1,
        previous_dispatch_tool_call_id: str = "",
    ) -> WorkerDispatchResult | None:
        req = WorkerDispatchRequest.from_dict(args)
        req = enrich_worker_dispatch_contract(req)

        # ── Work Artifact handling ───────────────────────────────────────────
        raw_artifact = args.get("work_artifact") if isinstance(args.get("work_artifact"), dict) else None

        if raw_artifact:
            # Multi-item artifact from Planner
            items_raw = raw_artifact.get("items") if isinstance(raw_artifact.get("items"), list) else []
            
            _log.info(
                "WorkArtifact parsed tool_call_id=%s item_count=%d",
                tool_call_id,
                len(items_raw),
            )
            
            # Attach the raw payload so DispatchProxy can create the full artifact
            req.work_artifact_payload = raw_artifact
            req.artifact_id = tool_call_id

        first_dispatch_in_turn = planner_dispatch_attempt <= 1
        chained_later_dispatch = not first_dispatch_in_turn
        _log.info(
            (
                "planner_dispatch_entry tool_call_id=%s "
                "dispatch_attempt=%s first_dispatch_in_turn=%s "
                "chained_later_dispatch=%s previous_dispatch_tool_call_id=%s"
            ),
            tool_call_id,
            planner_dispatch_attempt,
            first_dispatch_in_turn,
            chained_later_dispatch,
            previous_dispatch_tool_call_id,
        )
        if previous_dispatch_tool_call_id:
            result = _dispatch_chained_rejection(
                req,
                tool_call_id=tool_call_id,
                previous_tool_call_id=previous_dispatch_tool_call_id,
            )
            payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="dispatch_to_worker",
                    ok=False,
                    result=payload,
                    extras={
                        "dispatch": True,
                        "recoverable": True,
                        "summary": result.summary,
                        **result.extras,
                    },
                )
            )
            return result

        if dispatch_cb is None:
            err = (
                "dispatch_to_worker is not enabled for this manager - "
                "planner/worker mode is off."
            )
            payload = json.dumps({"ok": False, "error": err})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="dispatch_to_worker",
                    ok=False,
                    result=payload,
                )
            )
            return None

        on_event(
            WorkerDispatchRequested(
                tool_call_id=tool_call_id,
                goal=req.goal,
                files=list(req.files),
                spec=req.spec,
                acceptance=req.acceptance,
                summary=req.summary,
            )
        )
        try:
            result = dispatch_cb(tool_call_id, req)
        except Exception as exc:
            result = WorkerDispatchResult(
                ok=False,
                summary="Harness error due to an internal Worker dispatch exception.",
                cancelled=False,
                recoverable=False,
                extras={
                    "worker_internal_error": True,
                    "error_type": type(exc).__name__,
                    "internal_error": redact_secrets(f"{type(exc).__name__}: {exc}"),
                },
            )

        payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
        self._history.append_tool_result(tool_call_id, payload)
        event_extras = {
            "dispatch": True,
            "cancelled": result.cancelled,
            "summary": result.summary,
            "recoverable": result.recoverable,
            "phase_boundary": result.phase_boundary,
            "needs_followup": result.needs_followup,
            "followup_reason": result.followup_reason,
        }
        event_extras.update(result.extras)
        event_ok = result.ok or bool(result.recoverable and not result.cancelled)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="dispatch_to_worker",
                ok=event_ok,
                result=payload,
                extras=event_extras,
            )
        )
        return result

    def handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        mode: str,
        explicit_validation_commands: list[str] | None = None,
    ) -> dict[str, Any] | None:
        command = args.get("command", "")
        requested_command = str(command or "")
        if not command:
            payload = json.dumps({"ok": False, "error": "command is required"})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_terminal_command",
                    ok=False,
                    result=payload,
                )
            )
            return {"_terminal_payload": {"ok": False, "error": "command is required", "command": ""}}

        validation_command = parse_validation_command(
            requested_command,
            source="worker_command" if mode == "worker" else "single_command",
        )
        requested_cwd = str(args.get("cwd") or args.get("working_directory") or "").strip()
        try:
            if requested_cwd and validation_command.cwd:
                parsed_resolved = resolve_workspace_cwd(self._workspace_root, validation_command.cwd)
                requested_resolved = resolve_workspace_cwd(self._workspace_root, requested_cwd)
                if parsed_resolved != requested_resolved:
                    raise ValueError("cwd conflicts with command working directory")
                resolved_cwd = requested_resolved
            else:
                resolved_cwd = resolve_workspace_cwd(
                    self._workspace_root,
                    requested_cwd or validation_command.cwd,
                )
            relative_cwd = workspace_relative_cwd(self._workspace_root, resolved_cwd)
        except ValueError as exc:
            payload_dict = {
                "ok": False,
                "exit_code": None,
                "output": "",
                "command": validation_command.command,
                "requested_command": requested_command,
                "original_command": requested_command,
                "cwd": requested_cwd or validation_command.cwd,
                "working_directory": requested_cwd or validation_command.cwd,
                "failure_class": VALIDATION_COMMAND_UNRUNNABLE,
                "error": str(exc),
                "recoverable": True,
                "suggested_next_tool": "run_terminal_command",
                "suggested_next_action": (
                    "Use a workspace-relative cwd/working_directory that stays inside the workspace."
                ),
            }
            payload = json.dumps(payload_dict, ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_terminal_command",
                    ok=False,
                    result=payload,
                )
            )
            return {"_terminal_payload": payload_dict}

        if relative_cwd != validation_command.cwd:
            validation_command = replace(
                validation_command,
                cwd=relative_cwd,
                normalized=validation_command.normalized or bool(relative_cwd),
            )

        if mode == "worker" and validation_command.malformed:
            run_result = classify_validation_run(
                validation_command,
                exit_code=None,
                output="Validation text was not a runnable command.",
                ok=False,
            )
            payload_dict = {
                "ok": False,
                "exit_code": None,
                "output": run_result.output,
                "command": "",
                "requested_command": requested_command,
                "original_command": requested_command,
                "cwd": relative_cwd,
                "working_directory": relative_cwd,
                "failure_class": MALFORMED_VALIDATION_COMMAND,
            }
            payload_dict.update(run_result.metadata())
            payload = json.dumps(payload_dict, ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_terminal_command",
                    ok=False,
                    result=payload,
                )
            )
            return {"_terminal_payload": payload_dict}

        command = validation_command.command or requested_command

        if mode == "worker":
            explicit = matches_explicit_validation(
                str(command),
                explicit_validation_commands,
                cwd=relative_cwd,
            )
            decision = worker_terminal_command_allowed(
                str(command),
                explicit_validation_commands=explicit_validation_commands,
                workspace_root=self._workspace_root,
            )
            if not decision.allowed:
                blocked_payload = decision.to_blocked_payload(str(command))
                payload = json.dumps(blocked_payload, ensure_ascii=False)
                self._history.append_tool_result(tool_call_id, payload)
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name="run_terminal_command",
                        ok=False,
                        result=payload,
                    )
                )
                return {
                    "recoverable": True,
                    "phase_boundary": False,
                    "reason": decision.failure_class,
                    "_terminal_payload": blocked_payload,
                }

            command_plan = build_project_command(
                resolved_cwd,
                str(command),
                explicit=explicit,
            )
            if command_plan.missing_tool:
                blocked_payload = project_environment_missing_payload(
                    str(command),
                    command_plan.missing_tool,
                    explicit=explicit,
                    failure_class=command_plan.failure_class or "project_environment_missing_tool",
                    toolchain=command_plan.toolchain,
                )
                payload = json.dumps(blocked_payload, ensure_ascii=False)
                self._history.append_tool_result(tool_call_id, payload)
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name="run_terminal_command",
                        ok=False,
                        result=payload,
                    )
                )
                return {
                    "recoverable": True,
                    "phase_boundary": False,
                    "reason": command_plan.failure_class or "project_environment_missing_tool",
                    "_terminal_payload": blocked_payload,
                }
            command = command_plan.command
            original_command = command_plan.original_command or requested_command
        else:
            command_plan = build_project_command_rewrite(
                resolved_cwd,
                str(command),
            )
            command = command_plan.command
            original_command = command_plan.original_command or requested_command

        timeout = resolve_terminal_timeout(
            command,
            args.get("timeout"),
        )

        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_terminal_command"))

        settings = load_settings()
        sandbox = SandboxExecutor(
            mode=settings.sandbox_mode,  # type: ignore[arg-type]
            workspace_root=self._workspace_root,
            network_enabled=True,
        )

        output_lines: list[str] = []

        def on_output_chunk(text: str) -> None:
            output_lines.append(text)
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        result: SandboxResult = sandbox.run_terminal_command(
            command=command,
            timeout=timeout,
            cancel_event=cancel_event,
            on_output=on_output_chunk,
            working_directory=resolved_cwd,
        )

        full_output = result.stdout
        ok = result.exit_code != -1
        exit_code = result.exit_code

        if not ok and result.stderr and "Docker is not available" in result.stderr:
            full_output = f"[SANDBOX ERROR] {result.stderr}"

        payload_dict = {
            "ok": ok,
            "exit_code": exit_code,
            "output": full_output,
            "command": command,
            "requested_command": requested_command,
            "original_command": original_command,
            "cwd": relative_cwd,
            "working_directory": relative_cwd,
        }
        terminal_classification = classify_terminal_run(
            str(command),
            exit_code=exit_code,
            output=full_output,
        )
        payload_dict.update(terminal_classification.metadata())
        if validation_command.normalized:
            payload_dict.update(validation_command.metadata())
        should_classify_validation = (
            mode == "worker"
            and (
                explicit
                or validation_command.normalized
                or looks_like_validation_command(validation_command.command)
            )
        )
        stall: dict[str, Any] | None = None
        if should_classify_validation:
            run_result = classify_validation_run(
                validation_command,
                exit_code=exit_code,
                output=full_output,
                ok=ok,
            )
            payload_dict.update(run_result.metadata())
            stall = self._verification_tracker.observe(
                command=validation_command.command,
                classification=run_result.classification,
                output=full_output,
            )
            # Compute contextual command outcome so that Workers see
            # a classification that accounts for command role (e.g.
            # "passed" for validation commands that exit 0 despite
            # intermediate traceback output in a fallback branch).
            outcome = classify_command_outcome(
                command,
                exit_code=exit_code,
                output=full_output,
                is_validation_command=True,
            )
            payload_dict.update(outcome.metadata())
            # Override the raw terminal fields with the contextual
            # outcome so successful validation does not carry a scary
            # terminal_classification alongside validation_classification.
            payload_dict["terminal_classification"] = outcome.classification
            payload_dict["terminal_traceback_detected"] = outcome.traceback_detected
        payload = json.dumps(payload_dict, ensure_ascii=False)

        observed = self._loop_detector.observe(
            mode=mode,
            tool_name="run_terminal_command",
            args=args,
            ok=ok,
            content=payload,
        )
        payload = observed.content
        loop_info = observed.info
        if loop_info is None and stall is not None:
            loop_info = stall

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=ok,
                result=payload,
            )
        )
        if loop_info is None:
            loop_info = {}
        loop_info["_terminal_payload"] = payload_dict
        return loop_info

    def handle_run_and_watch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        declared_run_command: str,
    ) -> dict[str, Any]:
        if not declared_run_command or not declared_run_command.strip():
            payload_dict = {
                "ok": False,
                "failure_class": None,
                "error": "no run command declared for this task",
                "command": "",
            }
            payload = json.dumps(payload_dict, ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(ToolCallStart(index=0, id=tool_call_id, name="run_and_watch"))
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_and_watch",
                    ok=False,
                    result=payload,
                )
            )
            return {"_terminal_payload": payload_dict}

        window_seconds = 10
        raw_window = args.get("window_seconds")
        if raw_window is not None:
            try:
                window_seconds = max(1, min(int(raw_window), 60))
            except (TypeError, ValueError):
                pass

        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_and_watch"))

        sandbox = SandboxExecutor(
            mode="host",
            workspace_root=self._workspace_root,
            network_enabled=True,
        )

        output_lines: list[str] = []

        def on_output_chunk(text: str) -> None:
            output_lines.append(text)
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        result: WatchResult = sandbox.run_and_watch(
            command=declared_run_command,
            window_seconds=window_seconds,
            cancel_event=cancel_event,
            on_output=on_output_chunk,
        )

        full_output = result.output
        ok = result.ok and result.exited_early
        if not ok:
            if result.survived_window and not result.error_detected:
                failure_class = "launch_command_did_not_exit"
            elif result.error_detected:
                failure_class = "launch_command_crashed"
            elif result.exit_code is not None and result.exit_code != 0:
                failure_class = "launch_command_nonzero_exit"
            else:
                failure_class = "launch_command_failed"
        else:
            failure_class = None

        payload_dict = {
            "ok": ok,
            "failure_class": failure_class,
            "survived_window": result.survived_window,
            "exited_early": result.exited_early,
            "error_detected": result.error_detected,
            "exit_code": result.exit_code,
            "output": full_output,
            "command": declared_run_command,
        }
        payload = json.dumps(payload_dict, ensure_ascii=False)

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_and_watch",
                ok=ok,
                result=payload,
            )
        )

        return {"_terminal_payload": payload_dict}


def _dispatch_chained_rejection(
    req: WorkerDispatchRequest,
    *,
    tool_call_id: str,
    previous_tool_call_id: str,
) -> WorkerDispatchResult:
    failure_constraint = _dispatch_chained_constraint(
        previous_tool_call_id=previous_tool_call_id,
    )
    summary = (
        "The Worker was not started because Planner already dispatched a Worker "
        "in this user turn. Planner must include all required work in "
        "the original dispatch_to_worker work_artifact on retry or in a new user turn."
    )
    return WorkerDispatchResult(
        ok=False,
        summary=summary,
        needs_followup=True,
        recoverable=True,
        status=WorkerOutcomeStatus.needs_followup.value,
        extras={
            "dispatch_not_started": True,
            "planner_dispatch_chain_rejected": True,
            "planner_resolution_needed": True,
            "internal_planner_handoff": True,
            "user_visible_blocker": False,
            "failure_class": "planner_dispatch_already_used",
            "failure_constraint": failure_constraint,
            "tool_call_id": tool_call_id,
            "previous_dispatch_tool_call_id": previous_tool_call_id,
            "requested_goal": req.goal,
            "requested_files": list(req.files),
        },
    )


def _dispatch_chained_constraint(
    *,
    previous_tool_call_id: str,
) -> str:
    return "\n".join(
        [
            "CONSTRAINT FOR NEXT PLANNER ATTEMPT:",
            (
                "This planner turn already used dispatch_to_worker with "
                f"tool_call_id={previous_tool_call_id}."
            ),
            "Do not issue a second dispatch_to_worker call in the same user turn.",
            (
                "For multi-part work, include all required items in the "
                "original dispatch_to_worker work_artifact."
            ),
            "Items are bounded internal execution units and Aura executes them internally.",
            "Do not call edit/write tools.",
        ]
    )
