"""Run explicit validation commands as final gates before releasing a worker's
candidate final message.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aura.client import Event, ToolResult
from aura.sandbox import SandboxExecutor

EventCallback = Callable[[Event], None]


@dataclass
class WorkerFinalValidationResult:
    ok: bool
    gate: str = "explicit_validation"
    diagnostics: str = ""
    command: str = ""


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

    for command in commands:
        try:
            watch = sandbox.run_and_watch(command, window_seconds=window_seconds)
        except Exception as exc:
            return WorkerFinalValidationResult(
                ok=False,
                diagnostics=f"Exception running command: {type(exc).__name__}: {exc}",
                command=command,
            )

        ok = bool(watch.ok and watch.exited_early)
        if not ok:
            diagnostics = watch.output
            if not diagnostics.strip():
                diagnostics = (
                    "Command failed"
                    f" (exit_code={watch.exit_code}, "
                    f"survived_window={watch.survived_window}, "
                    f"exited_early={watch.exited_early})."
                )
            return WorkerFinalValidationResult(
                ok=False,
                diagnostics=diagnostics,
                command=command,
            )

    return WorkerFinalValidationResult(ok=True)


def emit_explicit_validation_result(
    *,
    command: str,
    ok: bool,
    output: str,
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:
    """Emit a ToolResult event for an explicit validation command failure."""
    payload = {
        "ok": ok,
        "command": command,
        "exit_code": 1 if not ok else 0,
        "output": output,
        "auto_validation": True,
        "verification_rung": "explicit_validation",
    }
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
]
