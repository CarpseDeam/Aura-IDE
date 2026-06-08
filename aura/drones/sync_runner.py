"""Lightweight synchronous Drone runner — no Qt dependency."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from aura.backends.api import APIAgentBackend
from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.config import get_provider, load_settings, resolve_role_default_model
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.registry import ToolRegistry
from aura.drones.definition import DroneDefinition, WRITE_TOOLS, default_tools_for_policy
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.project_env import build_project_command_rewrite
from aura.sandbox import SandboxExecutor
from aura.drones.store import RunHistoryStore

logger = logging.getLogger(__name__)


def _always_approve(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def run_read_only_drone_sync(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
) -> dict[str, Any]:
    """Run a read-only Drone synchronously and return a structured result dict.

    Args:
        workspace_root: Path to the workspace root.
        drone_id: The drone's id.
        drone: The DroneDefinition to execute.
        goal: The user's goal for this drone run.
        timeout_seconds: Maximum seconds before timing out.
        max_tool_rounds: Maximum tool-call rounds (overrides drone budget).

    Returns:
        dict with keys: ok, run_id, drone_id, drone_name, status, summary,
        tool_calls_made, tool_errors, elapsed_seconds.
    """
    run = DroneRun(drone=drone)
    run.mark("running")
    start_time = time.time()

    read_only = drone.write_policy == "read_only"
    registry = ToolRegistry(
        workspace_root=workspace_root,
        read_only=False,
        mode="single",
    )

    allowed_set = set(drone.allowed_tools or default_tools_for_policy(drone.write_policy))
    if read_only:
        allowed_set.difference_update(WRITE_TOOLS)

    tool_defs = registry.tool_defs()
    tool_defs = [
        t for t in tool_defs
        if t.get("function", {}).get("name") in allowed_set
    ]

    budget_min = max(1, timeout_seconds // 60)
    system_prompt = (
        f"You are a focused worker drone: \"{drone.name}\".\n\n"
        f"{drone.description}\n\n"
        f"## Instructions\n{drone.instructions}\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Rules\n"
        f"- Read-only mode: you cannot write or modify any files.\n"
        f"- Execute the task using the available tools.\n"
        f"- Provide a clear summary of what you found or accomplished.\n"
        f"- Keep responses concise and relevant.\n"
        f"- Budget: {max_tool_rounds} tool rounds, {budget_min} minute timeout.\n\n"
        f"## Output contract\n{drone.output_contract}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]

    provider_id = "deepseek"
    provider_cfg = get_provider(provider_id)
    model = resolve_role_default_model(provider_id, "worker") or provider_cfg.models.get("worker", "")

    backend = APIAgentBackend(provider=provider_id)
    cancel_event = threading.Event()

    tool_calls_made = 0
    tool_errors = 0
    content_parts: list[str] = []
    tool_call_records: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        for _round_num in range(max_tool_rounds):
            if cancel_event.is_set():
                run.mark("cancelled")
                break

            if time.time() - start_time > timeout_seconds:
                run.mark("timed_out")
                break

            stream = backend.stream(
                messages=messages,
                tools=tool_defs if tool_defs else None,
                model=model,
                thinking="off",
                cancel_event=cancel_event,
                temperature=0.7,
            )

            full_message: dict[str, Any] | None = None
            finish_reason: str | None = None

            for event in stream:
                if isinstance(event, ContentDelta):
                    content_parts.append(event.text)
                elif isinstance(event, ToolCallStart):
                    pass
                elif isinstance(event, ToolCallEnd):
                    pass
                elif isinstance(event, Usage):
                    pass
                elif isinstance(event, Done):
                    finish_reason = event.finish_reason
                    full_message = event.full_message
                elif isinstance(event, ApiError):
                    errors.append(event.message)
                    run.mark("failed")
                    break

            if run.status == "failed":
                break

            if finish_reason == "tool_calls" and full_message:
                tool_calls = full_message.get("tool_calls", [])
                if not tool_calls:
                    break

                messages.append(full_message)

                tool_results_content: list[dict[str, Any]] = []
                for tc in tool_calls:
                    tool_call_id = tc["id"]
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}

                    tool_calls_made += 1

                    try:
                        if name not in allowed_set:
                            ok = False
                            result_str = json.dumps(
                                {
                                    "ok": False,
                                    "error": f"tool not allowed for this Drone: {name}",
                                    "allowed_tools": sorted(allowed_set),
                                },
                                ensure_ascii=False,
                            )
                        elif name == "run_terminal_command":
                            ok, result_str = _execute_terminal_command(
                                workspace_root, args, timeout_seconds, cancel_event,
                            )
                        else:
                            result = registry.execute(
                                name,
                                args,
                                approval_cb=_always_approve,
                                reject_all=False,
                            )
                            ok = result.ok
                            result_str = result.to_tool_message_content()
                        if not ok:
                            tool_errors += 1
                    except Exception as exc:
                        ok = False
                        result_str = json.dumps({"error": str(exc)}, ensure_ascii=False)
                        errors.append(str(exc))
                        tool_errors += 1

                    tool_call_records.append(
                        {
                            "id": tool_call_id,
                            "name": name,
                            "args": args,
                            "ok": ok,
                            "result": result_str,
                        }
                    )

                    tool_results_content.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result_str,
                    })

                messages.extend(tool_results_content)
                continue

            elif finish_reason in ("stop", "end_turn", None):
                run.mark("completed")
                break
            else:
                run.mark("completed")
                break

        else:
            run.mark("completed")

    except Exception as exc:
        logger.exception("Sync drone runner error")
        run.mark("failed")
        errors.append(str(exc))

    ended = dt.datetime.now(dt.timezone.utc).isoformat()
    summary = "".join(content_parts).strip()
    elapsed = run.elapsed_seconds

    receipt = DroneReceipt(
        run_id=run.run_id,
        drone_id=drone.id,
        drone_name=drone.name,
        status=run.status,
        started_at=dt.datetime.fromtimestamp(run.started_at, tz=dt.timezone.utc).isoformat(),
        ended_at=ended,
        tool_calls_made=tool_calls_made,
        tool_errors=tool_errors,
        summary=summary,
        output_contract=drone.output_contract,
        tool_calls=tool_call_records,
        errors=errors,
        elapsed_seconds=elapsed,
    )

    try:
        RunHistoryStore.save_run(workspace_root, receipt)
    except Exception:
        logger.exception("Failed to save run receipt for %s", run.run_id)

    return {
        "ok": run.status in ("completed", "cancelled"),
        "run_id": run.run_id,
        "drone_id": drone.id,
        "drone_name": drone.name,
        "status": run.status,
        "summary": summary,
        "tool_calls_made": tool_calls_made,
        "tool_errors": tool_errors,
        "elapsed_seconds": elapsed,
        "receipt": receipt.to_dict(),
    }


def _execute_terminal_command(
    workspace_root: Path,
    args: dict[str, Any],
    drone_timeout: int,
    cancel_event: threading.Event,
) -> tuple[bool, str]:
    requested_command = str(args.get("command") or "").strip()
    if not requested_command:
        return False, json.dumps({"ok": False, "error": "command is required"}, ensure_ascii=False)

    command_plan = build_project_command_rewrite(workspace_root, requested_command)
    command = command_plan.command

    try:
        cmd_timeout = int(args.get("timeout", 0) or 0)
    except (TypeError, ValueError):
        cmd_timeout = 0
    if not cmd_timeout:
        cmd_timeout = min(45, drone_timeout)
    cmd_timeout = max(1, min(cmd_timeout, drone_timeout))

    settings = load_settings()
    sandbox = SandboxExecutor(
        mode=settings.sandbox_mode,
        workspace_root=workspace_root,
        network_enabled=True,
    )
    output_parts: list[str] = []

    def on_output(text: str) -> None:
        output_parts.append(text)

    result = sandbox.run_terminal_command(
        command=command,
        timeout=cmd_timeout,
        cancel_event=cancel_event,
        on_output=on_output,
    )
    output = result.stdout or "".join(output_parts)
    if not result.ok and result.stderr and "Docker is not available" in result.stderr:
        output = f"[SANDBOX ERROR] {result.stderr}"

    payload = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "output": output,
        "command": command,
        "requested_command": requested_command,
        "original_command": command_plan.original_command or requested_command,
    }
    return result.ok, json.dumps(payload, ensure_ascii=False)
