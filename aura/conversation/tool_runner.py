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
    ToolResult,
)
from aura.config import load_settings, redact_secrets
from aura.conversation.command_normalizer import normalize_command
from aura.conversation.history import History
from aura.conversation.tool_runner_terminal_policy import (
    matches_explicit_validation,
    resolve_terminal_timeout,
)
from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    VALIDATION_COMMAND_UNRUNNABLE,
    ValidationCommand,
    classify_command_outcome,
    classify_terminal_run,
    classify_validation_run,
    looks_like_validation_command,
    parse_validation_command,
)
from aura.project_env import (
    build_project_command_rewrite,
    resolve_workspace_cwd,
    workspace_relative_cwd,
)
from aura.sandbox import SandboxExecutor, SandboxResult, WatchResult
from aura.conversation.validation_orchestrator import ValidationCommandSpec

_log = logging.getLogger(__name__)


class ToolRunner:
    """Owns execution of terminal tools for production SINGLE."""

    def __init__(
        self,
        history: History,
        workspace_root: Path,
    ) -> None:
        self._history = history
        self._workspace_root = workspace_root

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
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

        if "\n" in requested_command or "\r" in requested_command:
            # A multiline command is a real shell script, not validation
            # prose.  ``parse_validation_command`` extracts a single command
            # line from a longer text; applying it here would truncate the
            # command at its first newline and silently drop the rest.
            validation_command = ValidationCommand(
                raw_text=requested_command,
                command=requested_command,
                source="single_command",
            )
        else:
            validation_command = parse_validation_command(
                requested_command,
                source="single_command",
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

        command = validation_command.command or requested_command

        # Normalize for shell-dialect validation (reject bare cd, export)
        # before the command reaches the sandbox.  The rewriting that
        # normalize_command does is redundant with the per-mode rewriting
        # below — the primary value here is the validity check.
        normalized = normalize_command(command, self._workspace_root)
        if not normalized.valid:
            payload_dict = {
                "ok": False,
                "exit_code": None,
                "output": normalized.validation_error,
                "command": command,
                "requested_command": requested_command,
                "original_command": requested_command,
                "cwd": relative_cwd,
                "working_directory": relative_cwd,
                "failure_class": VALIDATION_COMMAND_UNRUNNABLE,
                "error": normalized.validation_error,
                "recoverable": True,
                "suggested_next_tool": "run_terminal_command",
                "suggested_next_action": (
                    "Use the structured 'cwd' / 'working_directory' parameter "
                    "instead of bare 'cd', or chain the command with '&&'. "
                    "For environment variables, configure them through the "
                    "harness settings rather than 'export'."
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

        # Most project-environment rewriting is repeated by
        # build_project_command below.  Godot validation aliases are different:
        # they change the engine operation itself, so carry that corrected
        # command forward to the sandbox instead of validating one string and
        # accidentally executing the original one.
        if normalized.normalization_reason:
            command = normalized.command

        command_plan = build_project_command_rewrite(
            resolved_cwd,
            str(command),
        )
        command = command_plan.command
        original_command = command_plan.original_command or requested_command
        # Production single-agent mode owns its own validation, so its
        # terminal results carry the same validation classification the Worker
        # path produced. Without this, a genuinely passing validation has no
        # pass label and cannot be reported as proof.
        explicit = False
        is_ad_hoc_validation = looks_like_validation_command(str(command))

        timeout = resolve_terminal_timeout(
            command,
            args.get("timeout"),
        )

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
        ok = result.ok
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
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
        }
        if result.failure_class:
            payload_dict["failure_class"] = result.failure_class
        if normalized.normalization_reason:
            payload_dict.update(
                {
                    "normalized": True,
                    "validation_command_normalized": True,
                    "normalization_reason": normalized.normalization_reason,
                }
            )
        terminal_classification = classify_terminal_run(
            str(command),
            exit_code=exit_code,
            output=full_output,
            was_timeout=result.timed_out,
            execution_failed=result.failure_class == "execution_failed",
            cancelled=result.cancelled,
        )
        payload_dict.update(terminal_classification.metadata())
        if validation_command.normalized:
            payload_dict.update(validation_command.metadata())
        # In the structured validation-commands world, we first check
        # explicit spec matches; if none match, the ad-hoc
        # ``looks_like_validation_command`` fallback ensures validation
        # commands like ``python -m py_compile`` still count.
        should_classify_validation = (
            explicit or validation_command.normalized or is_ad_hoc_validation
        )
        if should_classify_validation:
            run_result = classify_validation_run(
                validation_command,
                exit_code=exit_code,
                output=full_output,
                ok=ok,
                failure_class=result.failure_class or "",
            )
            payload_dict.update(run_result.metadata())
            # Compute contextual command outcome so that the run sees
            # a classification that accounts for command role (e.g.
            # "passed" for validation commands that exit 0 despite
            # intermediate traceback output in a fallback branch).
            outcome = classify_command_outcome(
                command,
                exit_code=exit_code,
                output=full_output,
                is_validation_command=True,
                was_timeout=result.timed_out,
                execution_failed=result.failure_class == "execution_failed",
                cancelled=result.cancelled,
            )
            payload_dict.update(outcome.metadata())
            # Override the raw terminal fields with the contextual
            # outcome so successful validation does not carry a scary
            # terminal_classification alongside validation_classification.
            payload_dict["terminal_classification"] = outcome.classification
            payload_dict["terminal_traceback_detected"] = outcome.traceback_detected
        payload = json.dumps(payload_dict, ensure_ascii=False)

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=ok,
                result=payload,
            )
        )
        return {"_terminal_payload": payload_dict}

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

        # Normalize for consistent execution environment.
        normalized = normalize_command(declared_run_command, self._workspace_root)
        declared_run_command = normalized.command

        # Reject ambiguous shell constructs before they reach the sandbox.
        if not normalized.valid:
            payload_dict = {
                "ok": False,
                "failure_class": "validation_command_unrunnable",
                "error": normalized.validation_error,
                "command": declared_run_command,
            }
            payload = json.dumps(payload_dict, ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_and_watch",
                    ok=False,
                    result=payload,
                )
            )
            return {"_terminal_payload": payload_dict}

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
            if result.cancelled:
                failure_class = "cancelled"
            elif result.failure_class == "execution_failed":
                failure_class = "launch_command_execution_failed"
            elif result.survived_window and not result.error_detected:
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
            "cancelled": result.cancelled,
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
