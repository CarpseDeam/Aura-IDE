"""DroneRunner — executes a read-only Drone on a background QThread."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from aura.backends.api import APIAgentBackend
from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.config import get_provider, load_settings, resolve_role_default_model
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.tool_surface import build_drone_tool_surface
from aura.project_env import build_project_command_rewrite
from aura.sandbox import SandboxExecutor

logger = logging.getLogger(__name__)

DRONE_APPROVAL_TIMEOUT_SECONDS = 300.0


class DroneRunner(QObject):
    """Executes a single read-only Drone on a background thread.

    Usage:
        thread = QThread(self)
        runner = DroneRunner(workspace_root, drone)
        runner.moveToThread(thread)
        thread.started.connect(runner.run)
        runner.finished.connect(thread.quit)
        thread.start()
    """

    # --- Qt signals for the GUI thread ---
    statusChanged = Signal(str)            # status string: summoning/running/...
    contentDelta = Signal(str)             # text chunk from LLM
    toolCallStart = Signal(int, str, str)  # index, id, name
    toolCallArgsDelta = Signal(int, str)   # index, args_chunk
    toolCallEnd = Signal(int)              # index
    toolResult = Signal(str, str, bool, str)  # tool_call_id, name, ok, result
    usageEmitted = Signal(int, int, int, int)  # prompt, completion, cache_hit, cache_miss
    apiError = Signal(int, str)            # status_code, message
    receiptReady = Signal(object)          # DroneReceipt
    approval_requested = Signal(object)    # ApprovalRequest
    finished = Signal()

    def __init__(
        self,
        workspace_root: Path,
        drone: DroneDefinition,
        provider_id: str | None = None,
        model: str | None = None,
        auto_approve: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._drone = drone
        self._run = DroneRun(drone=drone)
        self._provider = provider_id
        self._model = model
        self._auto_approve = auto_approve
        self._approval_event: threading.Event | None = None
        self._approval_result: ApprovalDecision | None = None
        self._approval_id: str | None = None
        self._approval_lock = threading.Lock()
        self._reject_all: bool = False

    def cancel(self) -> None:
        """Request cancellation (thread-safe)."""
        self._run.cancel()

    @property
    def run_state(self) -> DroneRun:
        return self._run

    @Slot()
    def run(self) -> None:
        """Main execution method — runs on the QThread.

        Creates a read-only ToolRegistry, builds messages from the drone's
        instructions, loops through the agent backend until done or cancelled.
        """
        logger.info("Drone run started: %s (%s)", self._drone.name, self._run.run_id)
        self._run.mark("running")
        self.statusChanged.emit("running")
        self._reject_all = False

        # 1. Create a full registry, then expose only the Drone's saved tools.
        # Terminal commands are handled by ConversationManager in normal runs,
        # not ToolRegistry, so Drones need their own terminal execution path.
        read_only = self._drone.write_policy == "read_only"
        surface = build_drone_tool_surface(self._workspace_root, self._drone)
        registry = surface.registry
        allowed_set = set(surface.allowed_tools)
        tool_defs = list(surface.tool_defs)

        # 3. Build messages
        system_prompt = self._build_system_prompt(surface.setup_notes)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._drone.instructions},
        ]

        # 4. Resolve provider/model
        provider_id = self._provider or "deepseek"
        provider_cfg = get_provider(provider_id)
        model = self._model or resolve_role_default_model(provider_id, "worker") or provider_cfg.models.get("worker", "")

        # 5. Create backend
        backend = APIAgentBackend(provider=provider_id)

        # 6. Run the agent loop
        tool_calls_made = 0
        tool_errors = 0
        content_parts: list[str] = []
        tool_call_records: list[dict[str, Any]] = []
        pending_tool_args: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        max_rounds = self._drone.budget.max_tool_rounds
        timeout = self._drone.budget.timeout_seconds
        start_time = time.time()

        # Select approval callback based on write policy.
        if read_only:
            approval_cb = self._always_approve
        elif self._drone.write_policy == "normal_diff_approval" and self._auto_approve:
            approval_cb = self._always_approve
        else:
            approval_cb = self._build_approval_callback(errors)

        try:
            for _round_num in range(max_rounds):
                if self._run.cancel_event.is_set():
                    self._run.mark("cancelled")
                    self.statusChanged.emit("cancelled")
                    break

                if time.time() - start_time > timeout:
                    self._run.mark("timed_out")
                    self.statusChanged.emit("timed_out")
                    break

                # Call the LLM
                stream = backend.stream(
                    messages=messages,
                    tools=tool_defs if tool_defs else None,
                    model=model,
                    thinking="off",
                    cancel_event=self._run.cancel_event,
                    temperature=0.7,
                )

                full_message: dict[str, Any] | None = None
                finish_reason: str | None = None

                for event in stream:
                    if self._run.cancel_event.is_set():
                        break

                    if isinstance(event, ContentDelta):
                        content_parts.append(event.text)
                        self.contentDelta.emit(event.text)
                    elif isinstance(event, ReasoningDelta):
                        pass  # skip reasoning in drone output
                    elif isinstance(event, ToolCallStart):
                        pending_tool_args[event.id] = {
                            "name": event.name,
                            "index": event.index,
                            "args_text": "",
                        }
                        self.toolCallStart.emit(event.index, event.id, event.name)
                    elif isinstance(event, ToolCallArgsDelta):
                        for pending in pending_tool_args.values():
                            if pending.get("index") == event.index:
                                pending["args_text"] = str(pending.get("args_text", "")) + event.args_chunk
                                break
                        self.toolCallArgsDelta.emit(event.index, event.args_chunk)
                    elif isinstance(event, ToolCallEnd):
                        self.toolCallEnd.emit(event.index)
                    elif isinstance(event, Usage):
                        self.usageEmitted.emit(
                            event.prompt_tokens, event.completion_tokens,
                            event.cache_hit_tokens, event.cache_miss_tokens,
                        )
                    elif isinstance(event, Done):
                        finish_reason = event.finish_reason
                        full_message = event.full_message
                    elif isinstance(event, ApiError):
                        self.apiError.emit(event.status_code or -1, event.message)
                        errors.append(event.message)
                        self._run.mark("failed")
                        self.statusChanged.emit("failed")
                        break

                if self._run.cancel_event.is_set():
                    self._run.mark("cancelled")
                    self.statusChanged.emit("cancelled")
                    break

                if finish_reason == "tool_calls" and full_message:
                    tool_calls = full_message.get("tool_calls", [])
                    if not tool_calls:
                        # No more tool calls — done
                        break

                    # Append assistant message to history
                    messages.append(full_message)

                    # Execute each tool call
                    tool_results_content: list[dict[str, Any]] = []
                    for tc in tool_calls:
                        tool_call_id = tc["id"]
                        name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except (json.JSONDecodeError, KeyError):
                            args = {}

                        tool_calls_made += 1

                        # Execute via Drone-bounded tool surface.
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
                                if read_only:
                                    ok = False
                                    result_str = json.dumps(
                                        {
                                            "ok": False,
                                            "error": "terminal commands not allowed for read-only Drones",
                                        },
                                        ensure_ascii=False,
                                    )
                                else:
                                    ok, result_str = self._execute_terminal_command(args)
                            else:
                                result = registry.execute(
                                    name,
                                    args,
                                    approval_cb=approval_cb,
                                    reject_all=self._reject_all,
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

                        # Emit result for UI
                        self.toolResult.emit(tool_call_id, name, ok, result_str)
                        tool_call_records.append(
                            {
                                "id": tool_call_id,
                                "name": name,
                                "args": args,
                                "ok": ok,
                                "result": result_str,
                            }
                        )

                        # Build tool result for next API call
                        tool_results_content.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_str,
                        })

                    # Append tool results and continue loop
                    messages.extend(tool_results_content)
                    continue  # next round

                elif finish_reason in ("stop", "end_turn", None):
                    # Normal completion — no more tool calls
                    self._run.mark("completed")
                    self.statusChanged.emit("completed")
                    break

                else:
                    # Unknown finish_reason — treat as done
                    self._run.mark("completed")
                    self.statusChanged.emit("completed")
                    break

            else:
                # Loop exhausted without break — max rounds reached
                self._run.mark("completed")
                self.statusChanged.emit("completed")

        except Exception as exc:
            logger.exception("Drone runner error")
            if not self._run.cancel_event.is_set():
                self._run.mark("failed")
                self.statusChanged.emit("failed")
                self.apiError.emit(-1, str(exc))
                errors.append(str(exc))

        finally:
            # Build and emit receipt
            ended = dt.datetime.now(dt.timezone.utc).isoformat()
            summary = "".join(content_parts).strip()
            elapsed = self._run.elapsed_seconds
            receipt = DroneReceipt(
                run_id=self._run.run_id,
                drone_id=self._drone.id,
                drone_name=self._drone.name,
                status=self._run.status,
                started_at=dt.datetime.fromtimestamp(self._run.started_at, tz=dt.timezone.utc).isoformat(),
                ended_at=ended,
                tool_calls_made=tool_calls_made,
                tool_errors=tool_errors,
                summary=summary,
                output_contract=self._drone.output_contract,
                tool_calls=tool_call_records,
                errors=errors,
                elapsed_seconds=elapsed,
            )
            self.receiptReady.emit(receipt)
            self.finished.emit()

    def set_approval_result(self, decision: ApprovalDecision, approval_id: str | None = None) -> None:
        """Called from the GUI thread to unblock the worker with a decision."""
        with self._approval_lock:
            if self._approval_event is None:
                return
            if approval_id is not None and approval_id != self._approval_id:
                return
            self._approval_result = decision
            if decision.action in ("reject_all", "approve_all"):
                self._reject_all = (decision.action == "reject_all")
            self._approval_event.set()

    def _always_approve(self, _request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(action="approve")

    def _build_approval_callback(self, errors: list[str] | None = None):
        """Build a blocking callback that signals the GUI thread for approval."""
        def callback(request: ApprovalRequest) -> ApprovalDecision:
            approval_event = threading.Event()
            approval_id = f"{self._run.run_id}:{time.monotonic_ns()}"
            timeout_seconds = self._approval_timeout_seconds()
            request.approval_id = approval_id
            request.approval_timeout_seconds = timeout_seconds
            with self._approval_lock:
                self._approval_event = approval_event
                self._approval_result = None
                self._approval_id = approval_id
            self.approval_requested.emit(request)
            wait_result = self._wait_for_approval(approval_event, timeout_seconds)
            with self._approval_lock:
                decision = self._approval_result
                if self._approval_event is approval_event:
                    self._approval_event = None
                    self._approval_result = None
                    self._approval_id = None
            if wait_result == "approved":
                return decision or ApprovalDecision(action="reject")

            if wait_result == "cancelled":
                message = f"Drone diff approval cancelled for {request.tool_name} on {request.rel_path}."
                metadata = {
                    "approval_cancelled": True,
                    "failure_class": "approval_cancelled",
                    "tool_name": request.tool_name,
                    "rel_path": request.rel_path,
                }
            else:
                message = (
                    f"Drone diff approval timed out after {timeout_seconds:.0f}s "
                    f"for {request.tool_name} on {request.rel_path}."
                )
                metadata = {
                    "approval_timeout": True,
                    "failure_class": "approval_timeout",
                    "timeout_seconds": timeout_seconds,
                    "tool_name": request.tool_name,
                    "rel_path": request.rel_path,
                }
            if errors is not None:
                errors.append(message)
            logger.warning(message)
            return ApprovalDecision(
                action="reject",
                note=message,
                metadata=metadata,
            )
        return callback

    def _approval_timeout_seconds(self) -> float:
        """Return the bounded wait time for one Drone diff approval."""
        return max(0.001, min(DRONE_APPROVAL_TIMEOUT_SECONDS, float(self._drone.budget.timeout_seconds)))

    def _wait_for_approval(self, event: threading.Event, timeout_seconds: float) -> str:
        """Wait for approval, but wake promptly when the Drone is cancelled."""
        deadline = time.monotonic() + timeout_seconds
        while not self._run.cancel_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timed_out"
            if event.wait(timeout=min(0.2, remaining)):
                return "approved"
        return "cancelled"

    def _execute_terminal_command(self, args: dict[str, Any]) -> tuple[bool, str]:
        """Execute a bounded terminal command for a Drone run."""
        requested_command = str(args.get("command") or "").strip()
        if not requested_command:
            return False, json.dumps({"ok": False, "error": "command is required"}, ensure_ascii=False)

        command_plan = build_project_command_rewrite(self._workspace_root, requested_command)
        command = command_plan.command
        timeout = self._resolve_terminal_timeout(args.get("timeout"))
        settings = load_settings()
        sandbox = SandboxExecutor(
            mode=settings.sandbox_mode,  # type: ignore[arg-type]
            workspace_root=self._workspace_root,
            network_enabled=True,
        )
        output_parts: list[str] = []

        def on_output(text: str) -> None:
            output_parts.append(text)

        result = sandbox.run_terminal_command(
            command=command,
            timeout=timeout,
            cancel_event=self._run.cancel_event,
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

    def _resolve_terminal_timeout(self, raw_timeout: Any) -> int:
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            timeout = min(45, self._drone.budget.timeout_seconds)
        return max(1, min(timeout, self._drone.budget.timeout_seconds))

    def _build_system_prompt(self, setup_notes: tuple[str, ...] = ()) -> str:
        """Build the system prompt for this drone."""
        budget_min = max(1, self._drone.budget.timeout_seconds // 60)
        is_read_only = self._drone.write_policy == "read_only"
        if is_read_only:
            write_note = "- Read-only mode: you cannot write or modify any files."
        else:
            write_note = (
                "- Write-capable: you can read and write files. "
                "Write operations require your approval and will show a diff dialog. "
                "You can approve, reject, approve all, or reject all."
            )

        notes_section = ""
        if setup_notes:
            notes_section = "## Capability/setup notes\n" + "\n".join(f"- {n}" for n in setup_notes) + "\n\n"

        first_run_section = ""
        if self._drone.first_run_test:
            first_run_section = f"## First-run test\n{self._drone.first_run_test}\n\n"

        return (
            f"You are a focused worker drone: \"{self._drone.name}\".\n\n"
            f"{self._drone.description}\n\n"
            f"## Instructions\n{self._drone.instructions}\n\n"
            f"## Rules\n"
            f"{write_note}\n"
            f"- Execute the task using the available tools.\n"
            f"- Provide a clear summary of what you found or accomplished.\n"
            f"- Keep responses concise and relevant.\n"
            f"- Budget: {self._drone.budget.max_tool_rounds} tool rounds, {budget_min} minute timeout.\n\n"
            f"{notes_section}"
            f"{first_run_section}"
            f"## Output contract\n{self._drone.output_contract}"
        )
