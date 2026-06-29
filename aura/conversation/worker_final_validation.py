"""Run explicit validation commands as final gates before releasing a worker's
candidate final message.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aura.client import Event, ToolResult
from aura.conversation.validation_orchestrator import (
    VALIDATION_COMMAND_UNRUNNABLE,
    ValidationRunResult,
    classify_validation_run,
    parse_validation_command,
)
from aura.project_env import resolve_workspace_cwd
from aura.sandbox import SandboxExecutor

EventCallback = Callable[[Event], None]


@dataclass
class WorkerFinalValidationResult:
    ok: bool
    gate: str = "explicit_validation"
    diagnostics: str = ""
    command: str = ""
    runs: list[ValidationRunResult] | None = None

    @property
    def counts_as_product_failure(self) -> bool:
        return any(run.counts_as_product_failure for run in self.runs or [])


def run_explicit_validation_commands(
    *,
    workspace_root: Path,
    commands: list[str],
    window_seconds: int = 20,
) -> WorkerFinalValidationResult:
    """Run validation commands sequentially through SandboxExecutor.

    Stops at the first failure. Returns ok=True with empty diagnostics if all
    commands pass or if the command list is empty.
    """
    if not commands:
        return WorkerFinalValidationResult(ok=True)

    sandbox = SandboxExecutor(
        mode="host",
        workspace_root=workspace_root,
    )

    runs: list[ValidationRunResult] = []
    for raw_command in commands:
        validation_command = parse_validation_command(
            raw_command,
            source="explicit_task_command",
        )
        if validation_command.malformed:
            runs.append(
                classify_validation_run(
                    validation_command,
                    exit_code=None,
                    output="Validation text was not a runnable command.",
                    ok=False,
                )
            )
            continue
        command = validation_command.command
        try:
            working_directory = resolve_workspace_cwd(workspace_root, validation_command.cwd)
        except ValueError as exc:
            run = classify_validation_run(
                validation_command,
                exit_code=None,
                output=str(exc),
                ok=False,
                failure_class=VALIDATION_COMMAND_UNRUNNABLE,
            )
            runs.append(run)
            continue
        try:
            watch = sandbox.run_and_watch(
                command,
                window_seconds=window_seconds,
                working_directory=working_directory,
            )
        except Exception as exc:
            diagnostics = f"Exception running command: {type(exc).__name__}: {exc}"
            run = classify_validation_run(
                validation_command,
                exit_code=None,
                output=diagnostics,
                ok=False,
            )
            runs.append(run)
            return WorkerFinalValidationResult(
                ok=not run.counts_as_product_failure,
                diagnostics=diagnostics,
                command=command,
                runs=runs,
            )

        ok = bool(watch.ok and watch.exited_early)
        run = classify_validation_run(
            validation_command,
            exit_code=watch.exit_code,
            output=watch.output,
            ok=ok,
        )
        runs.append(run)
        if not ok:
            diagnostics = watch.output
            if not diagnostics.strip():
                diagnostics = (
                    "Command failed"
                    f" (exit_code={watch.exit_code}, "
                    f"survived_window={watch.survived_window}, "
                    f"exited_early={watch.exited_early})."
                )
            if not run.counts_as_product_failure:
                continue
            return WorkerFinalValidationResult(
                ok=False,
                diagnostics=diagnostics,
                command=command,
                runs=runs,
            )

    return WorkerFinalValidationResult(ok=True, runs=runs)


def emit_explicit_validation_result(
    *,
    command: str,
    ok: bool,
    output: str,
    on_event: EventCallback,
    workspace_root: str | Path,
    raw_text: str = "",
    expected_outcome: str = "",
    classification: str = "",
    counts_as_validation: bool | None = None,
    counts_as_product_failure: bool | None = None,
    user_action: str = "",
    normalized: bool = False,
    normalization_reason: str = "",
) -> None:
    """Emit a ToolResult event for an explicit validation command failure."""
    payload = {
        "ok": ok,
        "command": command,
        "exit_code": 1 if not ok else 0,
        "output": output,
        "auto_validation": True,
        "verification_rung": "explicit_validation",
        "validation_source": "explicit_task_command",
    }
    if raw_text:
        payload["validation_raw_text"] = raw_text
        payload["raw_text"] = raw_text
    if expected_outcome:
        payload["expected_outcome"] = expected_outcome
    if classification:
        payload["validation_classification"] = classification
        payload["classification"] = classification
    if counts_as_validation is not None:
        payload["counts_as_validation"] = counts_as_validation
    if counts_as_product_failure is not None:
        payload["counts_as_product_failure"] = counts_as_product_failure
    if user_action:
        payload["user_action"] = user_action
    payload["validation_command_normalized"] = normalized
    payload["normalized"] = normalized
    if normalization_reason:
        payload["normalization_reason"] = normalization_reason
    content = json.dumps(payload, ensure_ascii=False)
    on_event(
        ToolResult(
            tool_call_id="auto_explicit_validation",
            name="run_terminal_command",
            ok=ok,
            result=content,
            extras={"auto_validation": True, "verification_rung": "explicit_validation"},
        )
    )


def emit_explicit_validation_runs(
    *,
    runs: list[ValidationRunResult],
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:
    for run in runs:
        emit_explicit_validation_result(
            command=run.command,
            ok=run.ok,
            output=run.output,
            on_event=on_event,
            workspace_root=workspace_root,
            raw_text=run.raw_text,
            expected_outcome=run.expected_outcome,
            classification=run.classification,
            counts_as_validation=run.counts_as_validation,
            counts_as_product_failure=run.counts_as_product_failure,
            user_action=run.user_action,
            normalized=run.normalized,
            normalization_reason=run.normalization_reason,
        )


WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION = (
    "Final acceptance validation failed. Do not infer the expected value from prose first. "
    "Run a minimal diagnostic that prints the actual value(s), then patch only the failing code. "
    "After the patch, rerun the exact validation command. Finish only after it passes.\n\n"
    "Command: {command}\n\n"
    "Diagnostic output:\n{diagnostics}"
)


__all__ = [
    "WorkerFinalValidationResult",
    "run_explicit_validation_commands",
    "WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION",
    "emit_explicit_validation_result",
    "emit_explicit_validation_runs",
]
