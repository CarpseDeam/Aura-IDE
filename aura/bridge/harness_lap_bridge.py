from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEventLoop, QObject, QThread, Signal, Slot

from aura.backends import APIAgentBackend
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.dispatch import _DispatchProxy
from aura.bridge.lap_result import LapResult
from aura.config import redact_secrets
from aura.conversation import ConversationManager, History
from aura.conversation.tools import ToolRegistry
from aura.git_ops import changes_since, snapshot
from aura.hooks import hooks
from aura.models import DEFAULT_PLANNER_THINKING
from aura.prompts import (
    PLANNER_SYSTEM_PROMPT,
    build_tier1_context,
    inject_tier1_context,
)
from aura.settings import resolve_role_default_model

if TYPE_CHECKING:
    from aura.config import ProviderId

logger = logging.getLogger(__name__)


class _LapWorker(QObject):
    """Worker thread object that runs the planner conversation loop.
    Simplified for headless operation — no GUI signal forwarding.
    """

    finished = Signal()

    def __init__(
        self,
        manager: ConversationManager,
        approval_proxy: _ApprovalProxy,
        dispatch_proxy: _DispatchProxy | None,
        cancel_event: threading.Event,
        model: str,
        thinking: str,
        temperature: float = 0.7,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._approval_proxy = approval_proxy
        self._dispatch_proxy = dispatch_proxy
        self._cancel = cancel_event
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._workspace_root = workspace_root

    @Slot()
    def run(self) -> None:
        try:
            dispatch_cb = (
                self._dispatch_proxy.request_dispatch
                if self._dispatch_proxy is not None
                else None
            )
            self._manager.send(
                on_event=lambda ev: None,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                dispatch_cb=dispatch_cb,
                temperature=self._temperature,
                hook_name="generate_planner_code",
                max_tool_rounds=None,
            )
        except Exception as exc:
            logger.error(
                "Harness lap worker error: %s", redact_secrets(str(exc))
            )
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            self.finished.emit()


class HarnessLapBridge(QObject):
    """Headless, self-contained runner for unattended Drone harness laps.

    Owns its own History, ToolRegistry, ConversationManager, API backends
    (planner + worker), approval proxy, and dispatch proxy. Does NOT connect
    to any GUI signals. Manages global hook registration only during each lap.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        provider: str = "deepseek",
        planner_provider: str | None = None,
        planner_system_prompt: str = "",
    ) -> None:
        super().__init__()
        self._workspace_root = workspace_root
        self._provider: str = provider
        self._planner_provider = planner_provider
        self._planner_system_prompt = planner_system_prompt

        self._history = History()
        self._registry = ToolRegistry(
            workspace_root=workspace_root,
            mode="planner",
        )
        self._manager = ConversationManager(self._history, self._registry)

        self._planner_backend = APIAgentBackend(provider=planner_provider or provider)
        self._worker_backend = APIAgentBackend(provider=provider)

        self._approval_proxy = _ApprovalProxy(parent_widget=None)
        self._dispatch_proxy = _DispatchProxy(
            parent_widget=None,
            registry_factory=self._make_worker_registry,
            approval_proxy=self._approval_proxy,
            workspace_root=workspace_root,
            provider=provider,
        )

        # Build tier1 context once; reused across laps.
        self._tier1_context = (
            build_tier1_context(workspace_root) if workspace_root is not None else ""
        )

    def _make_worker_registry(self, mode: str) -> ToolRegistry:
        return ToolRegistry(
            workspace_root=self._registry.workspace_root,
            read_only=self._registry.read_only,
            mode="worker" if mode == "worker" else "single",
        )

    def run_one_lap(self, want: str) -> LapResult:
        """Execute one unattended planner -> worker lap.

        Saves and restores global hook registrations around the lap to avoid
        interfering with any visible ConversationBridge.
        """
        workspace_root = self._workspace_root

        # Save existing hook handlers
        saved_planner = hooks._handlers.get("generate_planner_code")
        saved_worker = hooks._handlers.get("generate_worker_code")
        hooks.unregister("generate_planner_code")
        hooks.unregister("generate_worker_code")
        hooks.register("generate_planner_code", self._planner_backend.stream)
        hooks.register("generate_worker_code", self._worker_backend.stream)

        old_approve_all = self._approval_proxy._approve_all_session
        old_registry_mode = self._registry.mode

        try:
            self._approval_proxy.set_approve_all_session(True)
            self._registry.set_mode("planner")

            # Reset and seed history
            self._history.messages.clear()
            self._dispatch_proxy.clear_records()
            self._history.append_user_text(want)

            base_prompt = (
                self._planner_system_prompt
                if self._planner_system_prompt
                else PLANNER_SYSTEM_PROMPT
            )
            self._manager.configure_for_planner(
                base_prompt=base_prompt,
                workspace_root=workspace_root,
            )
            self._history.set_system(
                inject_tier1_context(base_prompt, self._tier1_context)
            )

            # Git snapshot before lap
            pre_sha = snapshot(workspace_root) if workspace_root is not None else None

            planner_provider = self._planner_provider or self._provider
            model = resolve_role_default_model(planner_provider, "planner")
            thinking = DEFAULT_PLANNER_THINKING

            cancel = threading.Event()

            # Auto-dispatch: connect showSpecCard to user_dispatched
            self._dispatch_proxy.showSpecCard.connect(
                lambda tool_id, goal, files, spec, acceptance, summary, steps: (
                    self._dispatch_proxy.user_dispatched(
                        tool_id, goal, list(files), spec, acceptance, summary
                    )
                )
            )

            thread = QThread()
            worker = _LapWorker(
                manager=self._manager,
                approval_proxy=self._approval_proxy,
                dispatch_proxy=self._dispatch_proxy,
                cancel_event=cancel,
                model=model,
                thinking=thinking,
                temperature=0.7,
                workspace_root=workspace_root,
            )

            loop = QEventLoop()
            worker.finished.connect(loop.quit)
            worker.finished.connect(thread.quit)

            thread.started.connect(worker.run)
            thread.start()
            loop.exec()

            thread.wait(2000)
            thread.deleteLater()
            worker.deleteLater()

            self._dispatch_proxy.showSpecCard.disconnect()

            # Collect worker dispatch metadata
            worker_ok = True
            worker_status = "completed"
            worker_errors: list[str] = []
            validation_results: list[dict] = []
            try:
                from aura.conversation.worker_outcome import WorkerOutcomeStatus

                _SEVERITY = {
                    WorkerOutcomeStatus.completed.value: 0,
                    WorkerOutcomeStatus.completed_with_caveats.value: 1,
                    WorkerOutcomeStatus.validation_failed.value: 3,
                    WorkerOutcomeStatus.edit_mechanics_blocked.value: 4,
                    WorkerOutcomeStatus.harness_error.value: 5,
                }

                def _sev(s: str) -> int:
                    return _SEVERITY.get(s, -1)

                for record in self._dispatch_proxy.records():
                    meta = self._dispatch_proxy.result_metadata(
                        record.tool_call_id
                    )
                    if not meta:
                        continue
                    extras = meta.get("extras", {}) or {}
                    errs = extras.get("errors") or []
                    if errs:
                        worker_errors.extend(str(e) for e in errs)
                    vr = extras.get("validation_results") or []
                    if vr:
                        validation_results.extend(vr)

                    candidate: str | None = None
                    if extras.get("internal_error"):
                        candidate = WorkerOutcomeStatus.harness_error.value
                        if not worker_errors:
                            worker_errors.append(
                                str(extras["internal_error"])
                            )
                    elif extras.get("unrecovered_not_applied_writes"):
                        candidate = (
                            WorkerOutcomeStatus.edit_mechanics_blocked.value
                        )
                        if not worker_errors:
                            worker_errors.append(
                                "Unrecovered write failures"
                            )
                    else:
                        if vr and any(
                            r.get("exit_code") not in (0, None)
                            for r in vr
                        ):
                            candidate = (
                                WorkerOutcomeStatus.validation_failed.value
                            )
                            if not worker_errors:
                                worker_errors.append(
                                    "Validation command failed"
                                )
                        elif any(
                            "Validation command failed" in str(e)
                            for e in errs
                        ):
                            candidate = (
                                WorkerOutcomeStatus.validation_failed.value
                            )
                            if not worker_errors:
                                worker_errors.append(
                                    "Validation command failed"
                                )
                        elif extras.get("validation_not_run") and meta.get(
                            "modified_files"
                        ):
                            candidate = (
                                WorkerOutcomeStatus.validation_failed.value
                            )
                            if not worker_errors:
                                worker_errors.append(
                                    "Validation not run after writes"
                                )
                        elif extras.get("needs_followup"):
                            candidate = (
                                WorkerOutcomeStatus.harness_error.value
                            )
                            if not worker_errors:
                                worker_errors.append(
                                    "Worker reported needs_followup"
                                )

                    if candidate is not None and _sev(candidate) > _sev(
                        worker_status
                    ):
                        worker_status = candidate
                        worker_ok = False
            except Exception:
                logger.warning(
                    "Failed to collect worker dispatch metadata",
                    exc_info=True,
                )

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
                worker_ok=worker_ok,
                worker_status=worker_status,
                worker_errors=worker_errors,
                validation_results=validation_results,
            )
        finally:
            self._approval_proxy._approve_all_session = old_approve_all
            self._registry.set_mode(old_registry_mode)

            # Restore hook handlers
            hooks.unregister("generate_planner_code")
            hooks.unregister("generate_worker_code")
            if saved_planner:
                hooks.register("generate_planner_code", saved_planner)
            if saved_worker:
                hooks.register("generate_worker_code", saved_worker)
