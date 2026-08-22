from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QThread, Signal, Slot

from aura.backends import APIAgentBackend
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.lap_result import LapResult
from aura.bridge.production_execution import ProductionExecutionSession
from aura.client import ApiError
from aura.config import DEFAULT_THINKING, redact_secrets
from aura.context_gearbox.runtime import compose_system_prompt
from aura.conversation import ConversationManager, History
from aura.conversation.execution_outcome import ExecutionOutcomeStatus
from aura.conversation.tools import ToolRegistry
from aura.conversation.validation_truth import summarize_validation
from aura.git_ops import changes_since, snapshot
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.settings import resolve_production_default_model

logger = logging.getLogger(__name__)


class _LapConversationRunner(QObject):
    """Execution thread object that runs one production conversation lap.

    Simplified for headless operation — no GUI signal forwarding. The lap runs
    the ordinary production conversation loop against ``PRODUCTION_STREAM_HOOK``
    and reads the run's truthful terminal status directly off
    ``ProductionExecutionSession``'s structured execution ledgers.
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
        self.execution_ok: bool = True
        self.execution_status: str = ExecutionOutcomeStatus.completed.value

        production_session.executionFinished.connect(self._on_execution_finished)
        production_session.executionCancelled.connect(self._on_execution_cancelled)

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
        except Exception as exc:
            message = redact_secrets(str(exc))
            logger.error("Harness lap runner error: %s", message)
            self._production_session.handle_event(
                ApiError(status_code=None, message=message)
            )
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            try:
                self._production_session.finish(blocked_reason=self._blocked_reason)
            except Exception:
                logger.exception("Harness lap failed to resolve production execution finish")
            self.finished.emit()

    def _on_event(self, ev) -> None:
        self._production_session.handle_event(ev)

    def _on_execution_finished(self, run_id: str, ok: bool, status: str) -> None:
        self.execution_ok = ok
        self.execution_status = status

    def _on_execution_cancelled(self, run_id: str) -> None:
        self.execution_ok = False
        self.execution_status = ExecutionOutcomeStatus.cancelled.value


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
    ) -> None:
        super().__init__()
        self._workspace_root = workspace_root
        self._provider: str = provider

        self._history = History()
        self._registry = ToolRegistry(workspace_root=workspace_root)
        self._manager = ConversationManager(self._history, self._registry)

        self._production_backend = APIAgentBackend(provider=provider)

        self._approval_proxy = _ApprovalProxy(parent_widget=None)
        self._production_session = ProductionExecutionSession(
            approval_proxy=self._approval_proxy,
            parent=self,
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
            self._manager.reset_conversation_runtime()
            self._production_session.clear()
            self._history.append_user_text(want)

            model = resolve_production_default_model(self._provider)
            thinking = DEFAULT_THINKING

            self._manager.configure_runtime_context(
                workspace_root=workspace_root,
                model=model,
                content=want,
            )
            composed = compose_system_prompt(
                workspace_root,
                model=model,
                content=want,
                active_capabilities=self._registry.active_capabilities(),
            )
            self._history.set_system(composed.system_prompt)

            # Git snapshot before lap
            pre_sha = snapshot(workspace_root) if workspace_root is not None else None

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

            # Collect the run's truthful terminal status from the session's
            # structured execution ledgers — never a formatted report.
            relay = self._production_session.relay
            execution_ok = runner.execution_ok
            execution_status = runner.execution_status
            execution_errors: list[str] = [str(message) for message in relay.api_errors]
            if execution_status == ExecutionOutcomeStatus.harness_error.value and not execution_errors:
                blocked = runner._blocked_reason
                if blocked:
                    execution_errors.append(f"Blocked: {blocked}")
            for record in relay.not_applied_writes:
                path = str(record.get("path") or "")
                execution_errors.append(f"Write not applied: {path}")
            validation_results = [
                {
                    "command": outcome.command,
                    "attempts": outcome.attempts,
                    "passed": outcome.passed,
                    "repaired": outcome.repaired,
                    "exit_code": outcome.last_exit_code,
                }
                for outcome in summarize_validation(relay.validation_results)
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
