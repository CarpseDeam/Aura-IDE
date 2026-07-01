"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    Signal,
)

from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.dispatch_pending import _DispatchPending, DispatchPendingMap
from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.todo_controller import DispatchTodoController
from aura.bridge.worker_completion_result import (
    _check_read_before_edit,
    _last_assistant_content,
    prepare_worker_completion_result,
)
from aura.bridge.worker_relay_factory import create_worker_relay
from aura.bridge.worker_recording import _record_worker_completion
from aura.bridge.worker_report import (
    _build_worker_summary,
    _format_spec_as_user_message,
)
from aura.config import (
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt, context_gearbox_metadata
from aura.conversation import (
    ConversationManager,
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerTaskSpec,
    normalize_worker_task,
)
from aura.conversation.critic_dispatch import CriticCallback, CriticRequest, run_critic_dispatch
from aura.conversation.dispatch_plan import plan_from_request
from aura.conversation.persistence import WorkerDispatchRecord
from aura.conversation.project_profile import detect_project_profile
from aura.dependency_context import build_dependency_stanza
from aura.validation.selector import ValidationPlan
from aura.bridge.validation_selector_runtime import (
    build_worker_validation_selector_plan as _build_worker_validation_selector_plan,
    combine_validation_commands as _combine_validation_commands,
    refresh_validation_selector_plan,
    validation_selector_changed_files as _validation_selector_changed_files,
    validation_selector_commands as _validation_selector_commands,
)

_log = logging.getLogger(__name__)

__all__ = [
    "_DispatchProxy",
    "_DispatchPending",
    "_format_spec_as_user_message",
    "_build_worker_summary",
    "_last_assistant_content",
    "_check_read_before_edit",
]

DISPATCH_TIMEOUT = 300.0


class _DispatchProxy(QObject):
    showSpecCard = Signal(str, str, list, str, str, str, list)  # tool_id, goal, files, spec, acceptance, summary, steps
    workerStarted = Signal(str)  # tool_id
    workerFinished = Signal(str, bool, str, bool, str)  # tool_id, ok, summary, needs_followup, status
    workerCancelled = Signal(str)
    workerReasoningDelta = Signal(str, str)
    workerContentDelta = Signal(str, str)
    workerToolCallStart = Signal(str, str, str)  # parent_id, worker_tool_id, name
    workerToolCallArgs = Signal(str, str, str)
    workerToolCallEnd = Signal(str, str)
    workerToolResult = Signal(str, str, str, bool, str, dict)
    workerDiffDecided = Signal(str, str, str, str, str, str, bool)
    workerStreamDone = Signal(str, str, dict)
    workerApiError = Signal(str, int, str)
    workerUsage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    workerTodoListUpdated = Signal(str, list)  # tool_call_id, tasks
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    workerAgentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    workerAgentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        parent_widget,
        registry_factory,
        approval_proxy: _ApprovalProxy,
        workspace_root: Path | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._registry_factory = registry_factory
        self._approval_proxy = approval_proxy
        self._workspace_root = workspace_root
        self._provider = provider

        self._worker_model: ModelId = DEFAULT_WORKER_MODEL
        self._worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
        self._worker_temperature: float = 0.7
        self._worker_system_prompt: str = ""
        self._tier1_context: str = ""
        self._max_tool_rounds: int | None = None

        # Per-call state — the pending map owns its own lock so concurrent
        # dispatches (which shouldn't happen, but be safe) don't trample.
        self._pending_map = DispatchPendingMap()
        # Records of each completed dispatch for persistence.
        self._records: list[WorkerDispatchRecord] = []
        self._result_metadata: dict[str, dict[str, Any]] = {}
        self._todo_controller: DispatchTodoController = DispatchTodoController()

    # ---- config -----------------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def set_worker_model(self, model: ModelId) -> None:
        self._worker_model = model

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._worker_thinking = thinking

    def set_worker_temperature(self, temperature: float) -> None:
        self._worker_temperature = temperature

    def set_worker_system_prompt(self, prompt: str) -> None:
        self._worker_system_prompt = prompt

    def set_tier1_context(self, context: str) -> None:
        self._tier1_context = context

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)

    def set_max_tool_rounds(self, value: int | None) -> None:
        self._max_tool_rounds = value

    def records(self) -> list[WorkerDispatchRecord]:
        return list(self._records)

    def set_records(self, records: list[WorkerDispatchRecord]) -> None:
        self._records = list(records)

    def clear_records(self) -> None:
        self._records.clear()
        self._todo_controller.clear_all()

    def result_metadata(self, tool_call_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(tool_call_id, {}))

    # ---- canonical TODO controller ---------------------------------------

    def _emit_canonical_dispatch_todos(
        self, tool_call_id: str, tasks: list[dict[str, Any]]
    ) -> None:
        """Emit a canonical TODO snapshot to the GUI.

        This is the only path through which DispatchSession emits TODO updates
        during a canonical dispatch. It replaces raw Worker-local emissions.
        """
        self.workerTodoListUpdated.emit(tool_call_id, tasks)

    def _relay_worker_todo_update(self, tool_call_id: str, tasks: list) -> None:
        """Relay Worker-local TODO updates only outside canonical dispatch."""
        if self._todo_controller.has_canonical(tool_call_id):
            return
        # No canonical state: pass through as before (non-dispatch worker runs).
        self.workerTodoListUpdated.emit(tool_call_id, tasks)

    # ---- planner-thread side ---------------------------------------------

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        pending = self._pending_map.register(tool_call_id, req)

        # Tell GUI thread to render the spec card; user will call user_dispatched
        # or user_cancelled, which will set decision_event.
        self.showSpecCard.emit(
            tool_call_id,
            req.goal,
            list(req.files),
            req.spec,
            req.acceptance,
            req.summary,
            [step.to_dict() for step in req.steps],
        )

        signaled = pending.decision_event.wait(timeout=DISPATCH_TIMEOUT)
        if not signaled:
            self._pending_map.pop(tool_call_id)
            return WorkerDispatchResult(
                ok=False,
                recoverable=True,
                summary="Plan expired — click Dispatch again or Cancel",
                extras={"dispatch_not_started": True, "dispatch_approval_timeout": True},
            )

        if pending.cancelled:
            self._pending_map.pop(tool_call_id)
            return WorkerDispatchResult(
                ok=False,
                summary="Cancelled",
                cancelled=True,
                extras={"dispatch_not_started": True, "dispatch_cancelled": True},
            )

        edited = pending.edited_request or req

        # -- dependency graph: annotate downstream dependents ---------------
        if self._workspace_root is not None and edited.files:
            stanza = build_dependency_stanza(self._workspace_root, edited.files)
            if stanza:
                edited = replace(edited, spec=edited.spec + stanza)

        # Clear any prior canonical TODO state for this tool_call_id before
        # starting a new dispatch (new SpecCard → fresh checklist).
        self._todo_controller.clear(tool_call_id)

        plan = plan_from_request(edited)
        session = DispatchSession(
            tool_call_id=tool_call_id,
            original_request=edited,
            plan=plan,
            run_worker_step=self._run_worker,
            pending=pending,
            emit_todo_update=self._emit_canonical_dispatch_todos,
            emit_worker_started=self.workerStarted.emit,
            emit_worker_finished=self.workerFinished.emit,
            todo_controller=self._todo_controller,
        )
        # Run the session. The canonical TODO checklist survives after finish
        # so late Worker-local TODO events cannot repaint the rail. It is
        # cleared only on the next dispatch for this tool_call_id, cancellation,
        # or conversation reset.
        result = session.run()
        self._merge_session_result_metadata(tool_call_id, result)
        self._pending_map.pop(tool_call_id)
        return result
    # ---- GUI-thread side --------------------------------------------------

    def user_dispatched(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
    ) -> bool:
        if not self._pending_map.resolve_dispatched(
            tool_call_id,
            goal=goal,
            files=files,
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        ):
            logging.warning(
                f"user_dispatched: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        return True

    def _merge_session_result_metadata(
        self,
        tool_call_id: str,
        result: WorkerDispatchResult,
    ) -> None:
        if not isinstance(result.extras, dict) or not result.extras.get("dispatch_session"):
            return
        metadata = dict(self._result_metadata.get(tool_call_id, {}))
        extras = metadata.get("extras") if isinstance(metadata.get("extras"), dict) else {}
        metadata["extras"] = {**extras, **result.extras}
        if result.modified_files:
            metadata["modified_files"] = list(result.modified_files)
        if result.validation is not None:
            metadata["validation"] = result.validation
        self._result_metadata[tool_call_id] = metadata

    def user_cancelled(self, tool_call_id: str) -> bool:
        if not self._pending_map.resolve_cancelled(tool_call_id):
            logging.warning(
                f"user_cancelled: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        return True

    def cancel_all_pending(self) -> None:
        """Called when the user hits Stop. Unblocks any planner waiting for a
        dispatch decision AND signals any running worker to cancel."""
        if self._approval_proxy is not None:
            self._approval_proxy.cancel_active_dialog()
        self._pending_map.cancel_all()

    # ---- worker run ------

    def _run_worker(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        worker_history, task_spec, context_gearbox, worker_manager = self._prepare_worker_conversation(
            tool_call_id,
            req,
        )
        cancel_event = threading.Event()
        pending.cancel_event = cancel_event

        relay = self._create_worker_relay()
        (
            final_validation_commands,
            validation_selector,
            validation_selector_key,
            validation_selector_failed,
            internal_error,
            cleaned_scratch_files,
        ) = self._execute_worker_conversation(
            tool_call_id=tool_call_id,
            req=req,
            task_spec=task_spec,
            context_gearbox=context_gearbox,
            worker_manager=worker_manager,
            worker_history=worker_history,
            relay=relay,
            cancel_event=cancel_event,
        )

        worker_completion = prepare_worker_completion_result(
            req=req,
            worker_history=worker_history,
            task_spec=task_spec,
            relay=relay,
            context_gearbox=context_gearbox,
            internal_error=internal_error,
            cleaned_scratch_files=cleaned_scratch_files,
            final_validation_commands=final_validation_commands,
            workspace_root=self._workspace_root,
            preserve_scratch_records=_request_allows_root_check_files(req),
        )

        try:
            validation_selector, validation_selector_key, validation_selector_failed = (
                self._refresh_worker_validation_selector_plan(
                    relay=relay,
                    task_spec=task_spec,
                    task_kind=task_spec.task_shape.task_kind if task_spec.task_shape is not None else "unknown",
                    context_gearbox=context_gearbox,
                    final_validation_commands=final_validation_commands,
                    validation_selector=validation_selector,
                    validation_selector_key=validation_selector_key,
                    validation_selector_failed=validation_selector_failed,
                )
            )
        except Exception:
            _log.exception("Failed to build validation selector plan")

        completion_result = worker_completion.build_result(
            validation_selector=validation_selector,
        )

        _record_worker_completion(
            records=self._records,
            result_metadata=self._result_metadata,
            workspace_root=self._workspace_root,
            worker_model=str(self._worker_model),
            tool_call_id=tool_call_id,
            req=req,
            task_spec=task_spec,
            worker_history=worker_history,
            summary=completion_result.summary,
            modified_files=completion_result.modified_files,
            continuation=completion_result.continuation,
            extras=completion_result.extras,
            status=completion_result.status,
            structured_failure=completion_result.structured_failure,
            task_shape_summary=completion_result.task_shape_summary,
            result_errors=completion_result.result_errors,
            context_gearbox=context_gearbox,
        )

        return completion_result.result

    def _prepare_worker_conversation(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
    ) -> tuple[History, WorkerTaskSpec, dict[str, Any], ConversationManager]:
        worker_history = History()
        task_spec = normalize_worker_task(req)
        skill_content = _format_spec_as_user_message(task_spec)
        _log.info("worker_context_build_start tool_call_id=%s", tool_call_id)
        t1 = time.monotonic()
        composed_prompt = compose_system_prompt(
            RuntimeRole.WORKER,
            self._worker_system_prompt,
            self._workspace_root,
            model=str(self._worker_model),
            task_kind=task_spec.task_shape.task_kind if task_spec.task_shape is not None else None,
            target_files=tuple(task_spec.files),
            content=skill_content,
        )
        context_gearbox = context_gearbox_metadata(
            composed_prompt.ledger,
            workspace_root=self._workspace_root,
            task_kind=(
                task_spec.task_shape.task_kind
                if task_spec.task_shape is not None
                else None
            ),
        )
        self._tier1_context = composed_prompt.context_text
        _log.info(
            "worker_context_build_end tool_call_id=%s duration_ms=%.0f",
            tool_call_id, (time.monotonic() - t1) * 1000,
        )
        worker_history.set_system(composed_prompt.system_prompt)
        _log.info("worker_profile_detect_start tool_call_id=%s", tool_call_id)
        t2 = time.monotonic()
        if self._workspace_root is not None:
            try:
                profile = detect_project_profile(self._workspace_root)
                task_spec = replace(task_spec, project_profile=profile)
            except Exception:
                logging.exception("Failed to detect project profile for worker context")
        _log.info(
            "worker_profile_detect_end tool_call_id=%s duration_ms=%.0f",
            tool_call_id, (time.monotonic() - t2) * 1000,
        )
        base_message = _format_spec_as_user_message(task_spec)
        worker_history.append_user_text(base_message)

        worker_registry = self._registry_factory("worker")
        # Set the Planner contract on the worker's registry for contract gate checks
        if task_spec.contract is not None:
            worker_registry.set_contract(task_spec.contract)
        if task_spec.task_shape is not None and hasattr(worker_registry, "set_task_shape"):
            worker_registry.set_task_shape(task_spec.task_shape)
        worker_manager = ConversationManager(worker_history, worker_registry)
        return worker_history, task_spec, context_gearbox, worker_manager

    def _create_worker_relay(self) -> WorkerEventRelay:
        return create_worker_relay(
            approval_proxy=self._approval_proxy,
            worker_model=str(self._worker_model),
            dispatch_proxy=self,
            todo_relay_callback=self._relay_worker_todo_update,
        )

    def _refresh_worker_validation_selector_plan(
        self,
        *,
        relay: WorkerEventRelay,
        task_spec: WorkerTaskSpec,
        task_kind: str,
        context_gearbox: dict[str, Any],
        final_validation_commands: list[str],
        validation_selector: ValidationPlan | None,
        validation_selector_key: tuple[str, ...] | None,
        validation_selector_failed: bool,
    ) -> tuple[ValidationPlan | None, tuple[str, ...] | None, bool]:
        return refresh_validation_selector_plan(
            relay=relay,
            task_spec_validation_commands=task_spec.validation_commands,
            task_kind=task_kind,
            context_gearbox=context_gearbox,
            workspace_root=self._workspace_root,
            final_validation_commands=final_validation_commands,
            validation_selector=validation_selector,
            validation_selector_key=validation_selector_key,
            validation_selector_failed=validation_selector_failed,
        )

    def _execute_worker_conversation(
        self,
        *,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        task_spec: WorkerTaskSpec,
        context_gearbox: dict[str, Any],
        worker_manager: ConversationManager,
        worker_history: History,
        relay: WorkerEventRelay,
        cancel_event: threading.Event,
    ) -> tuple[list[str], ValidationPlan | None, tuple[str, ...] | None, bool, str | None, list[str]]:
        task_kind = task_spec.task_shape.task_kind if task_spec.task_shape is not None else "unknown"
        final_validation_commands = list(task_spec.validation_commands)
        validation_selector: ValidationPlan | None = None
        validation_selector_key: tuple[str, ...] | None = None
        validation_selector_failed = False

        def refresh_validation_selector_plan() -> None:
            nonlocal validation_selector, validation_selector_key, validation_selector_failed
            validation_selector, validation_selector_key, validation_selector_failed = (
                self._refresh_worker_validation_selector_plan(
                    relay=relay,
                    task_spec=task_spec,
                    task_kind=task_kind,
                    context_gearbox=context_gearbox,
                    final_validation_commands=final_validation_commands,
                    validation_selector=validation_selector,
                    validation_selector_key=validation_selector_key,
                    validation_selector_failed=validation_selector_failed,
                )
            )

        refresh_validation_selector_plan()

        def relay_worker_event(ev) -> None:
            relay.relay(tool_call_id, ev)
            refresh_validation_selector_plan()

        internal_error: str | None = None
        scratch_before = _validation_scratch_files(self._workspace_root) if self._workspace_root is not None else set()
        critic_cb = self._build_critic_callback(cancel_event=cancel_event)
        try:
            worker_manager.send(
                on_event=relay_worker_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=cancel_event,
                model=self._worker_model,
                thinking=self._worker_thinking,
                dispatch_cb=None,
                critic_cb=critic_cb,
                worker_dispatch_request=req,
                dispatch_tool_call_id=tool_call_id,
                temperature=self._worker_temperature,
                hook_name='generate_worker_code',
                max_tool_rounds=self._max_tool_rounds,
                explicit_validation_commands=final_validation_commands,
                declared_run_command=task_spec.run_command,
            )
        except Exception as exc:
            from aura.config import redact_secrets

            internal_error = redact_secrets(f"{type(exc).__name__}: {exc}")

        if cancel_event.is_set():
            worker_history.pop_if_empty_assistant_message()

        cleaned_scratch_files = self._cleanup_worker_scratch_outputs(req, relay, scratch_before)
        return (
            final_validation_commands,
            validation_selector,
            validation_selector_key,
            validation_selector_failed,
            internal_error,
            cleaned_scratch_files,
        )

    def _build_critic_callback(self, *, cancel_event: threading.Event) -> CriticCallback:
        def critic(tool_call_id: str, request: CriticRequest):
            # The hook registry currently wires provider streams as planner/worker.
            # Reuse the worker backend for model plumbing only; the critic gets no
            # tools and its events are never relayed to the user-facing worker stream.
            return run_critic_dispatch(
                tool_call_id,
                request,
                model=self._worker_model,
                thinking=self._worker_thinking,
                temperature=0.0,
                hook_name="generate_worker_code",
                cancel_event=cancel_event,
                tools=[],
            )

        return critic

    def _cleanup_worker_scratch_outputs(
        self,
        req: WorkerDispatchRequest,
        relay: WorkerEventRelay,
        scratch_before: set[Path],
    ) -> list[str]:
        if self._workspace_root is not None and not _request_allows_root_check_files(req):
            cleaned_scratch_files = _cleanup_new_validation_scratch_files(self._workspace_root, scratch_before)
            if cleaned_scratch_files:
                cleaned_set = set(cleaned_scratch_files)
                relay.write_results = [
                    item for item in relay.write_results if item.get("path") not in cleaned_set
                ]
                relay.touched_files.difference_update(cleaned_set)
                relay.wrote_new_files = [path for path in relay.wrote_new_files if path not in cleaned_set]
                relay.edited_existing_files = [
                    path for path in relay.edited_existing_files if path not in cleaned_set
                ]
            return cleaned_scratch_files
        return []


def _validation_scratch_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()

    files = set(_root_check_files(root))
    tmp_dir = root / ".aura" / "tmp"
    if tmp_dir.is_dir():
        for pattern in ("dump*.py", "_check*.py", "check*.py", "tmp*.py", "_tmp*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
            files.update(path for path in tmp_dir.glob(pattern) if path.is_file())
    return files


def _cleanup_new_validation_scratch_files(root: Path, before: set[Path]) -> list[str]:
    cleaned: list[str] = []
    for path in _validation_scratch_files(root):
        if path in before:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        cleaned.append(rel)
    return sorted(cleaned)


def _root_check_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()
    try:
        files: set[Path] = set()
        for pattern in ("_check*.py", "_tmp*.py", "tmp_*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
            files.update(path.resolve() for path in root.glob(pattern) if path.is_file())
        return files
    except OSError:
        return set()


def _request_allows_root_check_files(req: WorkerDispatchRequest) -> bool:
    text = " ".join([req.goal, req.spec, req.acceptance, req.summary]).lower()
    if "_check" in text or "_tmp" in text or "tmp_" in text:
        return True
    if "_inspect" in text or "_diagnostic" in text:
        return True
    return any(
        Path(path).name.startswith(("_check", "_tmp", "tmp_", "_inspect", "inspect", "diagnostic", "_diagnostic"))
        for path in req.files
    )


def _cleanup_new_root_check_files(root: Path, before: set[Path]) -> list[str]:
    cleaned: list[str] = []
    for path in _root_check_files(root):
        if path in before:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            path.unlink()
            cleaned.append(rel)
        except OSError:
            continue
    return cleaned
