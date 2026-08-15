"""Production ``run_terminal_command`` owner.

Argument validation, command preparation, persistent-session execution,
validation classification, history, and terminal events live here.  The
PowerShell process itself is owned by :class:`PowerShellSession`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from aura.client import TerminalCommandStarted, TerminalOutput, ToolResult
from aura.conversation.command_normalizer import normalize_command
from aura.conversation.history import History
from aura.conversation.tool_runner_terminal_policy import resolve_terminal_timeout
from aura.conversation.validation_orchestrator import (
    VALIDATION_COMMAND_UNRUNNABLE,
    ValidationCommand,
    ValidationCommandSpec,
    classify_command_outcome,
    classify_terminal_run,
    classify_validation_run,
    looks_like_validation_command,
    parse_validation_command,
)
from aura.project_env import (
    resolve_workspace_cwd,
    workspace_relative_cwd,
)
from aura.shell.powershell_session import PowerShellSession


class ShellTool:
    """Own one persistent model-facing PowerShell tool for a conversation."""

    def __init__(
        self,
        history: History,
        workspace_root: Path,
        *,
        session_factory: Callable[[Path], PowerShellSession] = PowerShellSession,
    ) -> None:
        self._history = history
        self._workspace_root = Path(workspace_root).resolve()
        self._session_factory = session_factory
        self._session = session_factory(self._workspace_root)

    @property
    def session(self) -> PowerShellSession:
        return self._session

    def set_workspace_root(self, root: Path) -> None:
        resolved = Path(root).resolve()
        if resolved == self._workspace_root:
            return
        self.close()
        self._workspace_root = resolved
        self._session = self._session_factory(resolved)

    def reset(self) -> None:
        """Reset the session at a conversation boundary."""
        self.close()
        self._session = self._session_factory(self._workspace_root)

    def close(self) -> None:
        self._session.close()

    def handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
    ) -> dict[str, Any] | None:
        requested_command = str(args.get("command") or "")
        if not requested_command:
            return self._emit_payload(
                tool_call_id,
                on_event,
                self._validation_error_payload(
                    command="",
                    requested_command="",
                    cwd="",
                    error="command is required",
                ),
            )

        if "\n" in requested_command or "\r" in requested_command:
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

        requested_cwd = str(
            args.get("cwd") or args.get("working_directory") or ""
        ).strip()
        try:
            if requested_cwd and validation_command.cwd:
                parsed_resolved = resolve_workspace_cwd(
                    self._workspace_root, validation_command.cwd
                )
                requested_resolved = resolve_workspace_cwd(
                    self._workspace_root, requested_cwd
                )
                if parsed_resolved != requested_resolved:
                    raise ValueError("cwd conflicts with command working directory")
                resolved_cwd = requested_resolved
            elif requested_cwd or validation_command.cwd:
                resolved_cwd = resolve_workspace_cwd(
                    self._workspace_root,
                    requested_cwd or validation_command.cwd,
                )
            else:
                resolved_cwd = self._session.current_cwd
            if not resolved_cwd.is_dir():
                raise ValueError("cwd must name an existing directory inside the workspace")
            relative_cwd = self._relative_cwd(resolved_cwd)
        except ValueError as exc:
            return self._emit_payload(
                tool_call_id,
                on_event,
                self._validation_error_payload(
                    command=validation_command.command,
                    requested_command=requested_command,
                    cwd=requested_cwd or validation_command.cwd,
                    error=str(exc),
                ),
            )

        if relative_cwd != validation_command.cwd:
            validation_command = replace(
                validation_command,
                cwd=relative_cwd,
                normalized=validation_command.normalized or bool(relative_cwd),
            )

        command = validation_command.command or requested_command
        normalized = normalize_command(
            command,
            self._workspace_root,
            allow_persistent_cd=True,
        )
        if not normalized.valid:
            return self._emit_payload(
                tool_call_id,
                on_event,
                self._validation_error_payload(
                    command=command,
                    requested_command=requested_command,
                    cwd=relative_cwd,
                    error=normalized.validation_error,
                    next_action=(
                        "Use PowerShell syntax supported by the persistent session. "
                        "For environment variables, use PowerShell variables or "
                        "Set-Item Env:NAME."
                    ),
                ),
            )

        command = normalized.command
        original_command = normalized.original_command or command
        timeout = resolve_terminal_timeout(command, args.get("timeout"))
        explicit = self._matches_explicit_validation(
            command, explicit_validation_commands, relative_cwd
        )
        is_ad_hoc_validation = looks_like_validation_command(str(command))

        session_cwd = str(self._session.current_cwd)
        starting_cwd = str(resolved_cwd)
        prepared_command = self._prepare_command(command, resolved_cwd, session_cwd)
        was_normalized = normalized.normalized or validation_command.normalized
        normalization_reasons = [
            reason
            for reason in (
                validation_command.normalization_reason,
                normalized.normalization_reason,
            )
            if reason
        ]
        normalization_reason = "; ".join(dict.fromkeys(normalization_reasons))

        output_lines: list[str] = []

        def on_output_chunk(text: str) -> None:
            output_lines.append(text)
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        def on_submitted() -> None:
            on_event(
                TerminalCommandStarted(
                    tool_call_id=tool_call_id,
                    command=prepared_command,
                    cwd=starting_cwd,
                )
            )

        result = self._session.execute(
            prepared_command,
            timeout=timeout,
            cancel_event=cancel_event,
            on_output=on_output_chunk,
            on_submitted=on_submitted,
        )
        full_output = result.output or "".join(output_lines)
        payload_dict: dict[str, Any] = {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "output": full_output,
            "command": command,
            "normalized_command": command,
            "prepared_command": prepared_command,
            "executed_command": prepared_command if result.submitted else "",
            "requested_command": requested_command,
            "original_command": original_command,
            "normalized": was_normalized,
            "submitted": result.submitted,
            "cwd": self._relative_or_absolute(result.cwd),
            "actual_cwd": result.cwd,
            "starting_cwd": starting_cwd,
            "working_directory": relative_cwd,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "session_identity": result.session_id,
            "session_started": result.session_started,
            "session_restarted": result.session_restarted,
            "session_reset": result.session_reset,
        }
        if result.failure_class:
            payload_dict["failure_class"] = result.failure_class
        if was_normalized:
            payload_dict["validation_command_normalized"] = True
        if normalization_reason:
            payload_dict["normalization_reason"] = normalization_reason

        terminal_classification = classify_terminal_run(
            str(command),
            exit_code=result.exit_code,
            output=full_output,
            was_timeout=result.timed_out,
            execution_failed=result.failure_class == "execution_failed",
            cancelled=result.cancelled,
        )
        payload_dict.update(terminal_classification.metadata())
        if validation_command.normalized:
            payload_dict.update(validation_command.metadata())
        if explicit or validation_command.normalized or is_ad_hoc_validation:
            run_result = classify_validation_run(
                validation_command,
                exit_code=result.exit_code,
                output=full_output,
                ok=result.ok,
                failure_class=result.failure_class or "",
            )
            payload_dict.update(run_result.metadata())
            outcome = classify_command_outcome(
                command,
                exit_code=result.exit_code,
                output=full_output,
                is_validation_command=True,
                was_timeout=result.timed_out,
                execution_failed=result.failure_class == "execution_failed",
                cancelled=result.cancelled,
            )
            payload_dict.update(outcome.metadata())
            payload_dict["terminal_classification"] = outcome.classification
            payload_dict["terminal_traceback_detected"] = outcome.traceback_detected
        payload_dict.update(
            {
                "cwd": self._relative_or_absolute(result.cwd),
                "actual_cwd": result.cwd,
                "starting_cwd": starting_cwd,
                "working_directory": relative_cwd,
                "session_identity": result.session_id,
                "session_started": result.session_started,
                "session_restarted": result.session_restarted,
                "session_reset": result.session_reset,
                "command": command,
                "normalized_command": command,
                "prepared_command": prepared_command,
                "executed_command": prepared_command if result.submitted else "",
                "requested_command": requested_command,
                "original_command": original_command,
                "normalized": was_normalized,
                "submitted": result.submitted,
            }
        )
        if normalization_reason:
            payload_dict["normalization_reason"] = normalization_reason
        return self._emit_payload(tool_call_id, on_event, payload_dict, ok=result.ok)

    def _emit_payload(
        self,
        tool_call_id: str,
        on_event: Any,
        payload_dict: dict[str, Any],
        *,
        ok: bool | None = None,
    ) -> dict[str, Any]:
        payload = json.dumps(payload_dict, ensure_ascii=False)
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=bool(payload_dict.get("ok")) if ok is None else ok,
                result=payload,
            )
        )
        return {"_terminal_payload": payload_dict}

    def _validation_error_payload(
        self,
        *,
        command: str,
        requested_command: str,
        cwd: str,
        error: str,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        actual_cwd = str(self._session.current_cwd)
        relative_actual_cwd = self._relative_or_absolute(actual_cwd)
        return {
            "ok": False,
            "exit_code": None,
            "output": "",
            "command": command,
            "normalized_command": command,
            "prepared_command": "",
            "executed_command": "",
            "requested_command": requested_command,
            "original_command": requested_command,
            "normalized": False,
            "submitted": False,
            "requested_cwd": cwd,
            "cwd": relative_actual_cwd,
            "actual_cwd": actual_cwd,
            "starting_cwd": actual_cwd,
            "working_directory": cwd,
            "failure_class": VALIDATION_COMMAND_UNRUNNABLE,
            "error": error,
            "recoverable": True,
            "suggested_next_tool": "run_terminal_command",
            "suggested_next_action": next_action
            or "Use a workspace-relative cwd/working_directory that stays inside the workspace.",
            "session_identity": self._session.session_id,
            "session_started": False,
            "session_restarted": False,
            "session_reset": False,
        }

    def _relative_cwd(self, cwd: Path) -> str:
        try:
            return workspace_relative_cwd(self._workspace_root, cwd)
        except ValueError:
            return str(cwd)

    def _relative_or_absolute(self, cwd: str) -> str:
        return self._relative_cwd(Path(cwd))

    @staticmethod
    def _prepare_command(command: str, resolved_cwd: Path, starting_cwd: str) -> str:
        if str(resolved_cwd) == starting_cwd:
            return command
        quoted = "'" + str(resolved_cwd).replace("'", "''") + "'"
        return f"Set-Location -LiteralPath {quoted} -ErrorAction Stop\n{command}"

    @staticmethod
    def _matches_explicit_validation(
        command: str,
        explicit_commands: list[ValidationCommandSpec] | None,
        cwd: str,
    ) -> bool:
        normalized = " ".join(str(command).strip().lower().split())
        normalized_cwd = str(cwd or "").strip().replace("\\", "/").strip("/")
        for spec in explicit_commands or []:
            spec_command = spec.command
            spec_cwd = spec.cwd
            if (
                normalized == " ".join(spec_command.strip().lower().split())
                and normalized_cwd
                == str(spec_cwd or "").strip().replace("\\", "/").strip("/")
            ):
                return True
        return False


__all__ = ["ShellTool"]
