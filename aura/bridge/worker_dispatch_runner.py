"""Worker execution pipeline for bridge dispatch."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_completion_result import prepare_worker_completion_result
from aura.bridge.worker_recording import _record_worker_completion
from aura.bridge.worker_relay_factory import create_worker_relay
from aura.bridge.worker_report import _format_spec_as_user_message
from aura.bridge.worker_scratch_cleanup import (
    _cleanup_new_validation_scratch_files,
    _request_allows_root_check_files,
    _validation_scratch_files,
)
from aura.bridge.worker_validation_selector_bridge import (
    _WorkerValidationSelectorBridge,
    refresh_worker_validation_selector_plan,
)
from aura.config import ModelId, ThinkingMode, redact_secrets
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt, context_gearbox_metadata
from aura.context_gearbox.sources import loaded_target_files
from aura.conversation import (
    ConversationManager,
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerTaskSpec,
    normalize_worker_task,
)
from aura.conversation.critic_dispatch import CriticCallback, CriticRequest, run_critic_dispatch
from aura.conversation.persistence import WorkerDispatchRecord
from aura.conversation.detected_validation import (
    merge_validation_commands,
    runnable_detected_validation_commands,
)
from aura.conversation.project_profile import detect_project_profile
from aura.events import EventBus
from aura.lifecycle import LifecycleHooks
from aura.validation.selector import ValidationPlan

_log = logging.getLogger(__name__)

__all__ = ["WorkerDispatchRunner"]


class WorkerDispatchRunner:
    """Owns one Worker execution pipeline for a bridge dispatch step."""

    def __init__(
        self,
        *,
        approval_proxy: Any,
        registry_factory: Callable[[str], Any],
        workspace_root: Path | None,
        worker_model: ModelId,
        worker_thinking: ThinkingMode,
        worker_temperature: float,
        worker_system_prompt: str,
        max_tool_rounds: int | None,
        dispatch_proxy: Any,
        records: list[WorkerDispatchRecord],
        result_metadata: dict[str, dict[str, Any]],
        event_bus: EventBus,
        lifecycle: LifecycleHooks | None = None,
        suppress_final_report_activity: bool = False,
        suppress_workflow_state_updates: bool = False,
        set_tier1_context: Callable[[str], None] | None = None,
    ) -> None:
        self._approval_proxy = approval_proxy
        self._registry_factory = registry_factory
        self._workspace_root = workspace_root
        self._worker_model = worker_model
        self._worker_thinking = worker_thinking
        self._worker_temperature = worker_temperature
        self._worker_system_prompt = worker_system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._dispatch_proxy = dispatch_proxy
        self._suppress_final_report_activity = suppress_final_report_activity
        self._suppress_workflow_state_updates = suppress_workflow_state_updates
        self._records = records
        self._result_metadata = result_metadata
        self._set_tier1_context = set_tier1_context
        self._event_bus = event_bus
        self._lifecycle = lifecycle

    def run_worker(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: Any,
        record_replayable: bool = True,
    ) -> WorkerDispatchResult:
        _log.info("WorkerDispatchRunner.run_worker entered tool_call_id=%s", tool_call_id)
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
                refresh_worker_validation_selector_plan(
                    relay=relay,
                    task_spec=task_spec,
                    task_kind=task_spec.task_shape.task_kind if task_spec.task_shape is not None else "unknown",
                    context_gearbox=context_gearbox,
                    workspace_root=self._workspace_root,
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
            replayable=record_replayable,
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
        loaded_targets = loaded_target_files(
            self._workspace_root,
            tuple(task_spec.files),
        )
        if loaded_targets:
            context_gearbox["loaded_target_files"] = list(loaded_targets)
        if self._set_tier1_context is not None:
            self._set_tier1_context(composed_prompt.context_text)
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
                profile = None

            # Merge detected runnable validation commands into the task spec.
            # This ensures detected project checks appear even when the Planner
            # did not echo them, without modifying Planner behavior.
            if profile is not None and profile.validation_commands:
                detected_runnable = runnable_detected_validation_commands(profile)
                if detected_runnable:
                    merged = merge_validation_commands(
                        task_spec.validation_commands,
                        detected_runnable,
                    )
                    task_spec = replace(task_spec, validation_commands=merged)
        _log.info(
            "worker_profile_detect_end tool_call_id=%s duration_ms=%.0f",
            tool_call_id, (time.monotonic() - t2) * 1000,
        )
        base_message = _format_spec_as_user_message(task_spec)
        worker_history.append_user_text(base_message)

        worker_registry = self._registry_factory("worker")
        # Set the Planner contract on the worker's registry for contract gate checks.
        if task_spec.contract is not None:
            worker_registry.set_contract(task_spec.contract)
        if task_spec.task_shape is not None and hasattr(worker_registry, "set_task_shape"):
            worker_registry.set_task_shape(task_spec.task_shape)
        worker_manager = ConversationManager(
            worker_history,
            worker_registry,
            lifecycle=self._lifecycle,
            event_bus=self._event_bus,
        )
        return worker_history, task_spec, context_gearbox, worker_manager

    def _create_worker_relay(self) -> WorkerEventRelay:
        return create_worker_relay(
            approval_proxy=self._approval_proxy,
            worker_model=str(self._worker_model),
            dispatch_proxy=self._dispatch_proxy,
            suppress_final_report_activity=self._suppress_final_report_activity,
            suppress_workflow_state_updates=self._suppress_workflow_state_updates,
            event_bus=self._event_bus,
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

        vs_bridge = _WorkerValidationSelectorBridge(
            task_spec=task_spec,
            task_kind=task_kind,
            context_gearbox=context_gearbox,
            workspace_root=self._workspace_root,
            final_validation_commands=final_validation_commands,
        )
        vs_bridge.refresh(relay)

        def relay_worker_event(ev) -> None:
            relay.relay(tool_call_id, ev)
            vs_bridge.refresh(relay)

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
                loaded_target_files=list(
                    context_gearbox.get("loaded_target_files") or []
                ),
                temperature=self._worker_temperature,
                hook_name='generate_worker_code',
                max_tool_rounds=self._max_tool_rounds,
                explicit_validation_commands=final_validation_commands,
                declared_run_command=task_spec.run_command,
            )
        except Exception as exc:
            internal_error = redact_secrets(f"{type(exc).__name__}: {exc}")

        if cancel_event.is_set():
            worker_history.pop_if_empty_assistant_message()

        cleaned_scratch_files = self._cleanup_worker_scratch_outputs(req, relay, scratch_before)
        return (
            final_validation_commands,
            vs_bridge.validation_selector,
            vs_bridge.validation_selector_key,
            vs_bridge.validation_selector_failed,
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
