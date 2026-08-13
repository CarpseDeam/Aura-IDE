from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QThread, Signal, Slot

from aura.backends import APIAgentBackend
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.lap_result import LapResult
from aura.bridge.production_execution import ProductionExecutionSession
from aura.bridge.production_receipt import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_COMPLETED_UNVERIFIED,
    STATUS_HARNESS_ERROR,
    STATUS_PROVIDER_CONTRACT_FAILURE,
    STATUS_VALIDATION_FAILED,
    ProductionReceipt,
)
from aura.config import DEFAULT_THINKING, redact_secrets
from aura.context_gearbox.runtime import PRODUCTION_SYSTEM_PROMPT
from aura.conversation import ConversationManager, History
from aura.conversation.execution_outcome import ExecutionOutcomeStatus
from aura.conversation.tools import ToolRegistry
from aura.git_ops import changes_since, snapshot
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.prompts import (
    build_tier1_context,
    inject_tier1_context,
)
from aura.settings import resolve_production_default_model

logger = logging.getLogger(__name__)


# Map a production receipt status onto the ExecutionOutcomeStatus vocabulary the
# Drone harness-lap consumers understand. Only the truly-successful receipts
# map to success; every failure class maps to a real failure status.
_RECEIPT_STATUS_TO_EXECUTION: dict[str, str] = {
    STATUS_COMPLETED: ExecutionOutcomeStatus.completed.value,
    STATUS_COMPLETED_UNVERIFIED: ExecutionOutcomeStatus.completed.value,
    STATUS_VALIDATION_FAILED: ExecutionOutcomeStatus.validation_failed.value,
    STATUS_CANCELLED: ExecutionOutcomeStatus.cancelled.value,
    STATUS_BLOCKED: ExecutionOutcomeStatus.harness_error.value,
    STATUS_HARNESS_ERROR: ExecutionOutcomeStatus.harness_error.value,
    STATUS_PROVIDER_CONTRACT_FAILURE: ExecutionOutcomeStatus.harness_error.value,
}


class _LapConversationRunner(QObject):
    """Execution thread object that runs one production conversation lap.

    Simplified for headless operation — no GUI signal forwarding. The lap runs
    the ordinary production conversation loop against ``PRODUCTION_STREAM_HOOK`` and
    collects the run's structured receipt via ``ProductionExecutionSession``.
    """

    finished = Signal()

    def __init__(
        self,
        manager: ConversationManager,
        approval_proxy: _ApprovalProxy,
        production_session: ProductionExecutionSession,
        cancel_event: threading.Event,
        model: str,
        thinking: str,
        temperature: float = 0.7,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._approval_proxy = approval_proxy
        self._production_session = production_session
        self._cancel = cancel_event
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._workspace_root = workspace_root
        self._blocked_reason: str = ""
        self._provider_contract_failure: bool = False
        self._already_satisfied: bool = False
        self._receipt: ProductionReceipt | None = None

    @Slot()
    def run(self) -> None:
        try:
            self._production_session.begin(model=str(self._model))
            self._manager.send(
                on_event=self._on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                temperature=self._temperature,
            )
            self._blocked_reason = self._manager.last_turn_blocked_reason
            self._provider_contract_failure = (
                self._manager.last_turn_provider_contract_failure
            )
            self._already_satisfied = self._manager.last_turn_already_satisfied
        except Exception as exc:
            logger.error(
                "Harness lap runner error: %s", redact_secrets(str(exc))
            )
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            try:
                self._receipt = self._production_session.finish(
                    blocked_reason=self._blocked_reason,
                    provider_contract_failure=self._provider_contract_failure,
                    already_satisfied=self._already_satisfied,
                )
            except Exception:
                logger.exception("Harness lap failed to build production receipt")
            self.finished.emit()

    def _on_event(self, ev) -> None:
        self._production_session.handle_event(ev)


class HarnessLapBridge(QObject):
    """Headless, self-contained runner for unattended Drone harness laps.

    Owns its own History, ToolRegistry, ConversationManager, one production
    backend, approval proxy, and production execution session. Does NOT connect
    to any GUI signals. Manages global hook registration only during each lap.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        provider: str = "deepseek",
        system_prompt: str = "",
    ) -> None:
        super().__init__()
        self._workspace_root = workspace_root
        self._provider: str = provider
        self._system_prompt = system_prompt

        self._history = History()
        self._registry = ToolRegistry(workspace_root=workspace_root)
        self._manager = ConversationManager(self._history, self._registry)

        self._production_backend = APIAgentBackend(provider=provider)

        self._approval_proxy = _ApprovalProxy(parent_widget=None)
        self._production_session = ProductionExecutionSession(
            approval_proxy=self._approval_proxy,
            parent=self,
        )

        # Build tier1 context once; reused across laps.
        self._tier1_context = (
            build_tier1_context(workspace_root) if workspace_root is not None else ""
        )

    def run_one_lap(self, want: str) -> LapResult:
        """Execute one unattended production conversation lap.

        Saves and restores global hook registration around the lap to avoid
        interfering with any visible ConversationBridge.
        """
        workspace_root = self._workspace_root

        # Save existing hook handler
        saved_production = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        model_streams.register(PRODUCTION_STREAM_HOOK, self._production_backend.stream)

        old_approve_all = self._approval_proxy._approve_all_session
        try:
            self._approval_proxy.set_approve_all_session(True)

            # Reset and seed history
            self._history.messages.clear()
            self._production_session.clear()
            self._history.append_user_text(want)

            base_prompt = (
                self._system_prompt
                if self._system_prompt
                else PRODUCTION_SYSTEM_PROMPT
            )
            self._manager.configure_runtime_context(
                base_prompt=base_prompt,
                workspace_root=workspace_root,
            )
            self._history.set_system(
                inject_tier1_context(base_prompt, self._tier1_context)
            )

            # Git snapshot before lap
            pre_sha = snapshot(workspace_root) if workspace_root is not None else None

            model = resolve_production_default_model(self._provider)
            thinking = DEFAULT_THINKING

            cancel = threading.Event()

            thread = QThread()
            runner = _LapConversationRunner(
                manager=self._manager,
                approval_proxy=self._approval_proxy,
                production_session=self._production_session,
                cancel_event=cancel,
                model=model,
                thinking=thinking,
                temperature=0.7,
                workspace_root=workspace_root,
            )

            loop = QEventLoop()
            runner.finished.connect(loop.quit)
            runner.finished.connect(thread.quit)

            thread.started.connect(runner.run)
            thread.start()
            loop.exec()

            thread.wait(2000)
            thread.deleteLater()
            runner.deleteLater()

            # Collect production receipt metadata
            execution_ok = True
            execution_status = "completed"
            execution_errors: list[str] = []
            validation_results: list[dict] = []
            if runner._receipt is not None:
                metadata = runner._receipt.metadata
                execution_status = _RECEIPT_STATUS_TO_EXECUTION.get(
                    runner._receipt.status, ExecutionOutcomeStatus.harness_error.value
                )
                execution_ok = runner._receipt.ok
                execution_errors = [
                    str(message) for message in metadata.get("api_errors") or []
                ]
                if runner._receipt.status == STATUS_BLOCKED:
                    blocked = metadata.get("blocked_reason")
                    if blocked:
                        execution_errors.append(f"Blocked: {blocked}")
                for record in metadata.get("not_applied_writes") or []:
                    execution_errors.append(f"Write not applied: {record}")
                if runner._receipt.status == STATUS_PROVIDER_CONTRACT_FAILURE:
                    execution_errors.append("Provider was unusable for the lap turn.")
                validation_results = [
                    {
                        "command": str(item.get("command", "")),
                        "attempts": int(item.get("attempts", 0)),
                        "passed": bool(item.get("passed", False)),
                        "repaired": bool(item.get("repaired", False)),
                        "exit_code": item.get("exit_code"),
                    }
                    for item in metadata.get("validation") or []
                ]

            # Detect git changes
            has_work = False
            changed_files: tuple[str, ...] = ()
            summary = ""

            if workspace_root is not None:
                has_work, changed_files = changes_since(
                    workspace_root, pre_sha
                )
            if has_work:
                names = [p.split("/")[-1] for p in changed_files[:3]]
                if len(changed_files) <= 3:
                    summary = (
                        f"Changed {len(changed_files)} file(s): "
                        f"{', '.join(names)}"
                    )
                else:
                    summary = (
                        f"Changed {len(changed_files)} file(s): "
                        f"{', '.join(names)}, ..."
                    )
            else:
                summary = "No changes since lap start."

            return LapResult(
                has_work=has_work,
                summary=summary,
                changed_files=changed_files,
                execution_ok=execution_ok,
                execution_status=execution_status,
                execution_errors=execution_errors,
                validation_results=validation_results,
            )
        finally:
            self._approval_proxy._approve_all_session = old_approve_all
            # Restore hook handler
            model_streams.unregister(PRODUCTION_STREAM_HOOK)
            if saved_production:
                model_streams.register(PRODUCTION_STREAM_HOOK, saved_production)
