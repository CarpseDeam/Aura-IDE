"""Focused py_compile and auto-validation emit helpers extracted from ConversationManager."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from aura.client import Event, ToolResult
from aura.conversation.path_utils import normalize_worker_path, is_validation_scratch_path
from aura.project_env import preferred_python_for_compile, quote_command_arg

EventCallback = Callable[[Event], None]


def run_focused_py_compile(
    paths: list[str],
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """Run python -m py_compile on each touched product Python file.

    Returns (all_succeeded, combined_output).
    Uses sys.executable, cwd=workspace root, timeout=30s.
    Normalizes paths safely (backslash/slash, strip leading "./").
    Preserves dot-prefixed directories like .aura.
    """
    if not paths:
        return True, ""
    python_exe = str(preferred_python_for_compile(Path(workspace_root)))
    outputs: list[str] = []
    all_ok = True
    for path in sorted(paths):
        normalized = normalize_worker_path(path)
        if is_validation_scratch_path(normalized):
            continue
        full_path = Path(workspace_root) / normalized
        if not full_path.exists():
            outputs.append(f"{normalized}: file not found — cannot py_compile")
            all_ok = False
            continue
        try:
            result = subprocess.run(
                [python_exe, "-m", "py_compile", str(full_path)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace_root,
            )
            if result.returncode != 0:
                all_ok = False
                err = result.stderr.strip() or result.stdout.strip() or "py_compile failed"
                outputs.append(f"{normalized}: {err}")
            else:
                outputs.append(f"{normalized}: ok")
        except subprocess.TimeoutExpired:
            all_ok = False
            outputs.append(f"{normalized}: timed out after 30s")
        except FileNotFoundError:
            all_ok = False
            outputs.append(f"{normalized}: project Python interpreter not found")
        except OSError as exc:
            all_ok = False
            outputs.append(f"{normalized}: OSError: {exc}")
    combined = "\n".join(outputs)
    return all_ok, combined


def emit_auto_py_compile_result(
    *,
    paths: list[str],
    ok: bool,
    diagnostics: str,
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:

    product_paths = [
        normalize_worker_path(path)
        for path in paths
        if not is_validation_scratch_path(path)
    ]
    if not product_paths:
        return
    python_exe = quote_command_arg(preferred_python_for_compile(Path(workspace_root)))
    command = python_exe + " -m py_compile " + " ".join(product_paths)
    payload = {
        "ok": ok,
        "command": command,
        "exit_code": 0 if ok else 1,
        "output": diagnostics,
        "auto_validation": True,
    }
    content = json.dumps(payload, ensure_ascii=False)
    on_event(
        ToolResult(
            tool_call_id="auto_py_compile",
            name="run_terminal_command",
            ok=ok,
            result=content,
        )
    )


def emit_auto_import_result(
    *,
    paths: list[str],
    diagnostics: str,
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:

    product_paths = [
        normalize_worker_path(path)
        for path in paths
        if not is_validation_scratch_path(path)
    ]
    if not product_paths:
        return
    python_exe = quote_command_arg(preferred_python_for_compile(Path(workspace_root)))
    command = python_exe + ' -c "import <module>"  # import verification'
    payload = {
        "ok": False,
        "command": command,
        "exit_code": 1,
        "output": diagnostics,
        "auto_validation": True,
        "verification_rung": "import",
    }
    content = json.dumps(payload, ensure_ascii=False)
    on_event(
        ToolResult(
            tool_call_id="auto_import_check",
            name="run_terminal_command",
            ok=False,
            result=content,
        )
    )


def emit_auto_dependent_import_info(
    *,
    paths: list[str],
    diagnostics: str,
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:

    product_paths = [
        normalize_worker_path(path)
        for path in paths
        if not is_validation_scratch_path(path)
    ]
    if not product_paths:
        return
    python_exe = quote_command_arg(preferred_python_for_compile(Path(workspace_root)))
    command = python_exe + ' -c "import <module>"  # import verification'
    payload = {
        "ok": True,
        "command": command,
        "exit_code": 0,
        "output": diagnostics,
        "auto_validation": True,
        "verification_rung": "dependent_import_info",
    }
    content = json.dumps(payload, ensure_ascii=False)
    on_event(
        ToolResult(
            tool_call_id="auto_dependent_import_info",
            name="run_terminal_command",
            ok=True,
            result=content,
        )
    )


def emit_auto_launch_result(
    *,
    command: str,
    ok: bool,
    output: str,
    on_event: EventCallback,
    workspace_root: str | Path,
) -> None:

    payload = {
        "ok": ok,
        "command": command,
        "exit_code": 1 if not ok else 0,
        "output": output,
        "auto_validation": True,
        "verification_rung": "launch",
    }
    content = json.dumps(payload, ensure_ascii=False)
    on_event(
        ToolResult(
            tool_call_id="auto_launch_check",
            name="run_terminal_command",
            ok=False,
            result=content,
        )
    )
