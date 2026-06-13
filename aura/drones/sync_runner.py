"""Lightweight synchronous Drone runner — no Qt dependency."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from aura.backends.api import APIAgentBackend
from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.config import get_provider
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.consequential import is_consequential
from aura.conversation.tools.registry import ToolRegistry
from aura.drones.contracts import BUILTIN_TYPES, ArtifactType, is_compatible
from aura.drones.definition import WRITE_TOOLS, DroneDefinition, default_tools_for_policy
from aura.drones.folder_runner import is_folder_backed_drone, run_folder_drone_sync
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import RunHistoryStore
from aura.project_env import build_project_command_rewrite
from aura.sandbox import SandboxExecutor
from aura.settings import load_settings, resolve_role_default_model

logger = logging.getLogger(__name__)


def _always_approve(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def _python_type_to_type_string(value: Any) -> str:
    """Map a Python value to our type string vocabulary."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "any"


def _extract_last_json_block(text: str) -> str | None:
    """Extract the last ```json ... ``` fenced block from *text*.

    Returns the raw JSON string (without fences) or None if not found.
    """
    FENCE = "```json"
    idx = text.rfind(FENCE)
    if idx == -1:
        return None
    start = idx + len(FENCE)
    end = text.find("```", start)
    if end == -1:
        return None
    block = text[start:end].strip()
    return block if block else None


def _extract_and_validate_artifact(
    text: str,
    declared_type: ArtifactType,
) -> tuple[dict | None, bool, str]:
    """Extract a fenced JSON block from *text*, parse it, and validate
    against *declared_type*.

    Returns (parsed_dict, is_valid, evidence_string).
    """
    fenced = _extract_last_json_block(text)
    if fenced is None:
        return None, False, "No ```json ... ``` block found in response."

    try:
        parsed = json.loads(fenced)
    except json.JSONDecodeError as exc:
        return None, False, f"JSON parse error: {exc}"

    runtime_schema = {
        k: _python_type_to_type_string(v)
        for k, v in parsed.items()
    }
    runtime_type = ArtifactType(name="_runtime", schema=runtime_schema)
    if is_compatible(runtime_type, declared_type):
        return parsed, True, f"Produced valid {declared_type.name} artifact."

    missing = [
        f for f in declared_type.schema
        if f not in runtime_schema
    ]
    type_mismatches = [
        f for f in declared_type.schema
        if f in runtime_schema
        and declared_type.schema[f] != "any"
        and runtime_schema[f] != "any"
        and runtime_schema[f] != declared_type.schema[f]
    ]
    details = []
    if missing:
        details.append(f"Missing required field(s): {', '.join(missing)}")
    if type_mismatches:
        details.append(f"Type mismatch on field(s): {', '.join(type_mismatches)}")
    evidence = "; ".join(details) if details else "Incompatible artifact schema."
    return None, False, evidence


# ── Approval helpers ────────────────────────────────────────────────


def _check_write_approval(
    tool_name: str,
    args: dict[str, Any],
    approval_callback: Callable[[ApprovalRequest], ApprovalDecision],
) -> tuple[bool, str | None]:
    """Check whether a consequential tool call is approved.

    Returns (approved, rejection_payload).
    If approved, rejection_payload is None.
    If rejected, rejection_payload is a JSON error string.
    """
    rel_path = str(args.get("path", "")) if isinstance(args.get("path"), (str, Path)) else ""
    new_content = args.get("content", "") if isinstance(args.get("content"), str) else str(args)

    req = ApprovalRequest(
        tool_name=tool_name,
        rel_path=rel_path,
        old_content="",
        new_content=new_content,
        is_new_file=bool(rel_path),
    )
    decision = approval_callback(req)
    if decision.action in ("approve", "approve_all"):
        return True, None
    payload = {
        "ok": False,
        "error": f"Tool '{tool_name}' was not approved.",
        "action": decision.action,
        "note": decision.note or "",
    }
    return False, json.dumps(payload, ensure_ascii=False)


# ── Shared implementation ───────────────────────────────────────────


def _run_drone_sync_impl(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    *,
    write_enabled: bool = False,
    approval_callback: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
) -> dict[str, Any]:
    """Shared sync-drone execution for read-only and write-capable runners.

    Parameters
    ----------
    write_enabled:
        If True, write tools are available (subject to per-call approval).
    approval_callback:
        Per-write-operation approval callback for consequential tools.
    """
    run = DroneRun(drone=drone)
    run.mark("running")
    start_time = time.time()

    registry = ToolRegistry(
        workspace_root=workspace_root,
        read_only=False,
        mode="single",
    )

    allowed_set = set(drone.allowed_tools or default_tools_for_policy(drone.write_policy))
    if not write_enabled:
        allowed_set.difference_update(WRITE_TOOLS)

    tool_defs = registry.tool_defs()
    tool_defs = [
        t for t in tool_defs
        if t.get("function", {}).get("name") in allowed_set
    ]

    budget_min = max(1, timeout_seconds // 60)
    if not write_enabled:
        mode_line = "- Read-only mode: you cannot write or modify any files.\n"
    else:
        mode_line = "- Write mode: you can create and modify files.\n"

    system_prompt = (
        f"You are a focused worker drone: \"{drone.name}\".\n\n"
        f"{drone.description}\n\n"
        f"## Instructions\n{drone.instructions}\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Rules\n"
        f"{mode_line}"
        f"Execute the task using the available tools.\n"
        f"Provide a clear summary of what you found or accomplished.\n"
        f"Keep responses concise and relevant.\n"
        f"Budget: {max_tool_rounds} tool rounds, {budget_min} minute timeout.\n\n"
        f"## Output contract\n{drone.output_contract}"
    )

    # Artifact contract injection
    if drone.produces and drone.produces in BUILTIN_TYPES:
        art = BUILTIN_TYPES[drone.produces]
        schema_lines = "\n".join(
            f'  "{k}": "{v}"' for k, v in art.schema.items()
        )
        system_prompt += (
            f"\n\n## Output contract (structured)\n"
            f"You MUST end your response with a fenced JSON block conforming\n"
            f"to the following schema.\n"
            f"\nExpected type: **{art.name}**\n"
            f"Schema:\n"
            f"```\n{{\n{schema_lines}\n}}\n```\n"
            f"\nThe JSON block MUST be the LAST thing in your response, fenced\n"
            f"as ```json ... ```.  This is your sole output format for this run.\n"
            f"Every field in the schema is required.\n"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]

    provider_id_str = "deepseek"
    try:
        settings = load_settings()
        provider_id_str = settings.worker_provider or "deepseek"
    except Exception:
        provider_id_str = "deepseek"

    try:
        model = resolve_role_default_model(provider_id_str, "worker")
    except Exception:
        model = "deepseek-chat"
    if not model:
        try:
            provider_cfg = get_provider(provider_id_str)
            model = provider_cfg.models.get("worker", "deepseek-chat")
        except Exception:
            model = "deepseek-chat"

    backend = APIAgentBackend(provider=provider_id_str)
    cancel_event = threading.Event()

    tool_calls_made = 0
    tool_errors = 0
    approved_write_actions = 0
    rejected_write_actions = 0
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
                        elif write_enabled and is_consequential(name):
                            if approval_callback is not None:
                                approved, rejection_msg = _check_write_approval(
                                    name, args, approval_callback,
                                )
                                if approved:
                                    approved_write_actions += 1
                                    if name == "run_terminal_command":
                                        ok, result_str = _execute_terminal_command(
                                            workspace_root, args, timeout_seconds, cancel_event,
                                        )
                                    else:
                                        result = registry.execute(
                                            name, args,
                                            approval_cb=_always_approve,
                                            reject_all=False,
                                        )
                                        ok = result.ok
                                        result_str = result.to_tool_message_content()
                                else:
                                    rejected_write_actions += 1
                                    ok = False
                                    result_str = rejection_msg
                            else:
                                # No callback — auto-reject
                                rejected_write_actions += 1
                                ok = False
                                result_str = json.dumps(
                                    {
                                        "ok": False,
                                        "error": f"Tool '{name}' requires approval but no approval callback provided.",
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

    # ── Artifact extraction ────────────────────────────────────────
    produced_artifact: dict | None = None
    met: bool | None = None
    evidence: str = ""
    if drone.produces and drone.produces in BUILTIN_TYPES:
        declared_type = BUILTIN_TYPES[drone.produces]

        produced_artifact, met, evidence = _extract_and_validate_artifact(
            summary, declared_type
        )

        # ── Bounded repair loop ────────────────────────────
        MAX_REPAIRS = 2
        repair_attempt = 0
        while (
            met is False
            and repair_attempt < MAX_REPAIRS
            and run.status == "completed"
            and not cancel_event.is_set()
        ):
            repair_attempt += 1
            logger.info(
                "Artifact repair attempt %d/%d for drone '%s': %s",
                repair_attempt, MAX_REPAIRS, drone.id, evidence,
            )

            # Build correction prompt
            schema_lines = "\n".join(
                f'  "{k}": "{v}"' for k, v in declared_type.schema.items()
            )
            correction = (
                f"Your previous response had a problem with the structured output:\n\n"
                f"**Error:** {evidence}\n\n"
                f"**Expected artifact type:** {declared_type.name}\n"
                f"**Expected schema:**\n"
                f"```\n{{\n{schema_lines}\n}}\n```\n\n"
                f"Return ONLY the corrected final fenced ```json block "
                f"matching this schema exactly."
            )
            messages.append({"role": "user", "content": correction})

            try:
                repair_parts: list[str] = []
                stream = backend.stream(
                    messages=messages,
                    tools=None,           # No tools — output repair only
                    model=model,
                    thinking="off",
                    cancel_event=cancel_event,
                    temperature=0.7,
                )
                for event in stream:
                    if isinstance(event, ContentDelta):
                        repair_parts.append(event.text)
                    elif isinstance(event, ApiError):
                        errors.append(event.message)
                        break
                    # Done/Usage/ToolCallStart/ToolCallEnd — ignore, generator exhausts

                repair_text = "".join(repair_parts).strip()
                if repair_text:
                    # Try extraction on just the repair response
                    repaired_artifact, repaired_met, repaired_evidence = (
                        _extract_and_validate_artifact(repair_text, declared_type)
                    )
                    if repaired_met:
                        produced_artifact = repaired_artifact
                        met = True
                        evidence = (
                            f"Produced valid {drone.produces} artifact "
                            f"(repaired on attempt {repair_attempt})."
                        )
                        # Append repair to summary for receipts
                        content_parts.append(
                            f"\n\n[Repair attempt {repair_attempt}]\n{repair_text}"
                        )
                        break
                    else:
                        # Update evidence for the next attempt
                        evidence = repaired_evidence
                        content_parts.append(
                            f"\n\n[Repair attempt {repair_attempt}]\n{repair_text}"
                        )
                else:
                    evidence = f"Repair attempt {repair_attempt}: empty response from model."
            except Exception as exc:
                logger.exception("Artifact repair attempt %d crashed", repair_attempt)
                evidence = f"Repair attempt {repair_attempt} crashed: {exc}"
                # Don't break — next attempt may still work if within budget

    # Recompute summary for receipt (includes any repair text)
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
        produced_artifact=produced_artifact,
        met=met,
        evidence=evidence,
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
        "approved_write_actions": approved_write_actions,
        "rejected_write_actions": rejected_write_actions,
    }


# ── Public API ──────────────────────────────────────────────────────


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
        tool_calls_made, tool_errors, elapsed_seconds, receipt,
        approved_write_actions, rejected_write_actions.
    """
    if is_folder_backed_drone(drone):
        return run_folder_drone_sync(workspace_root, drone_id, drone, goal)

    return _run_drone_sync_impl(
        workspace_root, drone_id, drone, goal,
        write_enabled=False,
        timeout_seconds=timeout_seconds,
        max_tool_rounds=max_tool_rounds,
    )


def run_write_capable_drone_sync(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    *,
    approval_callback: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
) -> dict[str, Any]:
    """Run a write-capable Drone synchronously with per-call approval.

    Write tools are available (not filtered out).  Before each consequential
    tool call, *approval_callback* is consulted.  If no callback is provided,
    consequential tools are auto-rejected.

    Parameters
    ----------
    workspace_root:
        Path to the workspace root.
    drone_id:
        The drone's id.
    drone:
        The DroneDefinition to execute.
    goal:
        The user's goal for this drone run.
    approval_callback:
        Per-write-operation approval callback for consequential tools.
        If *None*, consequential tools are auto-rejected.
    timeout_seconds:
        Maximum seconds before timing out.
    max_tool_rounds:
        Maximum tool-call rounds (overrides drone budget).

    Returns
    -------
    dict with keys: ok, run_id, drone_id, drone_name, status, summary,
    tool_calls_made, tool_errors, elapsed_seconds, receipt,
    approved_write_actions, rejected_write_actions.
    """
    if is_folder_backed_drone(drone):
        return run_folder_drone_sync(workspace_root, drone_id, drone, goal)

    return _run_drone_sync_impl(
        workspace_root, drone_id, drone, goal,
        write_enabled=True,
        approval_callback=approval_callback,
        timeout_seconds=timeout_seconds,
        max_tool_rounds=max_tool_rounds,
    )


# ── Tool execution helpers ──────────────────────────────────────────


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
