"""GUI-thread coordination of saved Workflow cards and their explicit actions."""

from __future__ import annotations

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QInputDialog

from aura.agents.graph_store import AgentGraphStoreError
from aura.agents.local_state import AgentLocalStateError
from aura.agents.retention import AgentRetentionError
from aura.agents.store import AgentStoreError
from aura.agents.workflow_document import WorkflowSaved

_EDIT_ERRORS = (AgentGraphStoreError, AgentStoreError, AgentLocalStateError, AgentRetentionError)


class WorkflowChatController(QObject):
    def __init__(self, *, bridge, chat, owner, submit_run, parent_widget=None, parent=None):
        super().__init__(parent)
        self._bridge, self._chat, self._owner = bridge, chat, owner
        self._submit_run = submit_run
        self._parent_widget = parent_widget
        self._cards = {}
        bridge.workflowAuthored.connect(self._on_saved)
        bridge.started.connect(self._on_started)
        bridge.finished.connect(self._on_finished)
        chat.transientCardsCleared.connect(self.clear)
        owner.workflows_changed.connect(self.refresh)

    @property
    def cards(self):
        return tuple(self._cards.values())

    @Slot(object)
    def _on_saved(self, saved) -> None:
        if not isinstance(saved, WorkflowSaved):
            return
        workflow_id = saved.document.graph.graph_id
        card = self._cards.get(workflow_id)
        if card is None:
            card = self._chat.add_workflow_card(saved)
            self._cards[workflow_id] = card
            card.open_requested.connect(lambda: self._open(workflow_id))
            card.run_requested.connect(lambda: self._run(workflow_id))
            card.undo_requested.connect(lambda: self._undo(workflow_id))
        else:
            card.set_saved(saved)
            # Move the live preview to the current response after a chat edit.
            self._chat.current_assistant().add_activity_widget(card)
        self._set_busy(card)
        self._owner.refresh()

    def _set_busy(self, card) -> None:
        card.set_busy(
            self._bridge.is_running(),
            can_mutate=not self._bridge.requested_read_only,
        )

    @Slot()
    def _on_started(self) -> None:
        for card in self.cards:
            card.set_busy(True)

    @Slot()
    def _on_finished(self) -> None:
        self._owner.refresh()
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        if not self._cards or self._bridge.is_running():
            return
        service = self._owner.capture_workflow_authoring()
        if service is None:
            return
        for workflow_id, card in tuple(self._cards.items()):
            try:
                document = service.document(workflow_id)
                changed = document.revision != card.saved.document.revision
                card.set_saved(
                    WorkflowSaved(
                        document,
                        "Updated" if changed else card.saved.status,
                        service.edits.history(document.graph).can_undo,
                    )
                )
                self._set_busy(card)
            except _EDIT_ERRORS as exc:
                card.show_error(str(exc))
                card.run_button.setEnabled(False)
                card.undo_button.hide()

    def _open(self, workflow_id: str) -> None:
        card = self._cards.get(workflow_id)
        if card is None:
            return
        try:
            self._owner.open_workflow(workflow_id)
        except _EDIT_ERRORS as exc:
            card.show_error(str(exc))

    def _run(self, workflow_id: str) -> None:
        card = self._cards.get(workflow_id)
        if card is None or self._bridge.is_running():
            return
        task, accepted = QInputDialog.getMultiLineText(
            self._parent_widget,
            f"Run {card.saved.document.graph.name}",
            "What should this Workflow do?",
            "",
        )
        if not accepted or not task.strip():
            return
        try:
            self._submit_run(workflow_id, task.strip())
        except _EDIT_ERRORS as exc:
            card.show_error(str(exc))

    def _undo(self, workflow_id: str) -> None:
        card = self._cards.get(workflow_id)
        if card is None or self._bridge.is_running() or self._bridge.requested_read_only:
            return
        service = self._owner.capture_workflow_authoring()
        if service is None:
            return
        try:
            saved = service.undo(workflow_id, card.saved.document.revision)
            card.set_saved(saved)
            self._owner.refresh()
        except _EDIT_ERRORS as exc:
            card.show_error(str(exc))

    @Slot()
    def clear(self) -> None:
        self._cards.clear()
