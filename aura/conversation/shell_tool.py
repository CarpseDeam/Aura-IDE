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
    build_project_command_rewrite,
    resolve_workspace_cwd,
    workspace_relative_cwd,
)
from aura.shell.powershell_session import PowerShellCommandResult, PowerShellSession


class ShellTool:
    """Own one persistent model-facing PowerShell tool for a conversation."""

    def __init__(
        self,
        history: History,
        workspace_root: Path,
        *,
        legacy_executor_factory: Callable[..., Any] | None = None,
        settings_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._history = history
        self._workspace_root = Path(workspace_root).resolve()
        self._legacy_executor_factory = legacy_executor_factory
        self._settings_loader = settings_loader
        self._session = PowerShellSession(self._workspace_root)

    @property
    def session(self) -> PowerShellSession:
        return self._session

    def set_workspace_root(self, root: Path) -> None:
        resolved = Path(root).resolve()
        if resolved == self._workspace_root:
            return
        self.close()
        self._workspace_root = resolved
        self._session = PowerShellSession(resolved)

    def reset(self) -> None:
        """Reset the session at a conversation boundary."""
        self.close()

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
                {"ok": False, "error": "command is required", "command": ""},
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
        normalized = normalize_command(command, self._workspace_root)
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
                        "Use the structured 'cwd' / 'working_directory' parameter "
                        "instead of bare 'cd', or chain the command with '&&'. "
                        "For environment variables, use PowerShell variables or "
                        "Set-Item Env:NAME within the persistent session."
                    ),
                ),
            )

        if normalized.normalization_reason:
            command = normalized.command
        command_plan = build_project_command_rewrite(self._workspace_root, command)
        command = command_plan.command
        original_command = command_plan.original_command or requested_command
        timeout = resolve_terminal_timeout(command, args.get("timeout"))
        explicit = self._matches_explicit_validation(
            command, explicit_validation_commands, relative_cwd
        )
        is_ad_hoc_validation = looks_like_validation_command(str(command))

        starting_cwd = str(self._session.current_cwd)
        prepared_command = self._prepare_command(command, resolved_cwd, starting_cwd)
        # This is the only production terminal-start fact.  It is emitted after
        # normalization and immediately before the live session submission.
        on_event(
            TerminalCommandStarted(
                tool_call_id=tool_call_id,
                command=prepared_command,
                cwd=starting_cwd,
            )
        )

        output_lines: list[str] = []

        def on_output_chunk(text: str) -> None:
            output_lines.append(text)
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        if self._legacy_executor_factory is None:
            result = self._session.execute(
                prepared_command,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output_chunk,
            )
        else:
            # Compatibility seam for existing unit tests that replace the
            # old one-shot executor.  It is unreachable in production, where
            # ToolRunner always supplies the persistent session path.
            settings = self._settings_loader() if self._settings_loader else None
            executor = self._legacy_executor_factory(
                mode=getattr(settings, "sandbox_mode", "host"),
                workspace_root=self._workspace_root,
                network_enabled=True,
            )
            one_shot = executor.run_terminal_command(
                command=command,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output_chunk,
                working_directory=resolved_cwd,
            )
            result = PowerShellCommandResult(
                ok=bool(one_shot.ok),
                stdout=str(one_shot.stdout or ""),
                stderr=str(one_shot.stderr or ""),
                output=str(one_shot.stdout or ""),
                exit_code=one_shot.exit_code,
                cwd=str(resolved_cwd),
                timed_out=bool(getattr(one_shot, "timed_out", False)),
                cancelled=bool(getattr(one_shot, "cancelled", False)),
                failure_class=getattr(one_shot, "failure_class", None),
            )
        full_output = result.output or "".join(output_lines)
        payload_dict: dict[str, Any] = {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "output": full_output,
            "command": command,
            "prepared_command": prepared_command,
            "requested_command": requested_command,
            "original_command": original_command,
            "cwd": self._relative_or_absolute(result.cwd),
            "actual_cwd": result.cwd,
            "working_directory": relative_cwd,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "session_identity": result.session_id,
            "session_reset": result.session_reset,
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
        return {
            "ok": False,
            "exit_code": None,
            "output": "",
            "command": command,
            "requested_command": requested_command,
            "original_command": requested_command,
            "cwd": cwd,
            "working_directory": cwd,
            "failure_class": VALIDATION_COMMAND_UNRUNNABLE,
            "error": error,
            "recoverable": True,
            "suggested_next_tool": "run_terminal_command",
            "suggested_next_action": next_action
            or "Use a workspace-relative cwd/working_directory that stays inside the workspace.",
            "session_identity": self._session.session_id,
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
        return f"Set-Location -LiteralPath {quoted}\n{command}"

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
