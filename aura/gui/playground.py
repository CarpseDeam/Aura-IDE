from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal

_log = logging.getLogger(__name__)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.editor.file_edit_projection import FileEditProjection
from aura.gui.theme import BORDER
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.workspace_tree import WorkspaceTree


class AuraPlayground(QWidget):
    """Right-side workspace panel with code editor (top) and progress pane (bottom).

    Uses a vertical QSplitter to divide the space between a tabbed code editor
    pane and the checklist-only info hub (Progress) pane. Terminal output is
    routed to a floating TerminalWindow so it does not participate in this
    layout.
    """

    focused_action_requested = Signal(str)
    stop_execution_requested = Signal()

    def __init__(self, parent=None, terminal_window_geometry: str = "", outer_splitter_sizes: list[int] | None = None, vertical_splitter_sizes: list[int] | None = None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Flat QVBoxLayout — no outer HBox or _content_widget wrapper
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header_container = QWidget(self)
        header_container.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header_container)
        header_layout.setContentsMargins(12, 0, 12, 4)
        header_layout.setSpacing(8)

        self._header_label = QLabel("WORKSPACE", self)
        self._header_label.setObjectName("paneTitleWorkspace")
        header_layout.addWidget(self._header_label)

        header_layout.addStretch(1)

        self._close_all_btn = QToolButton(self)
        self._close_all_btn.setText("Close All")
        self._close_all_btn.setObjectName("closeAllBtn")
        self._close_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_all_btn.setToolTip("Close all workspace tabs and clear execution output.")
        self._close_all_btn.setAccessibleName("Close all workspace output")
        self._close_all_btn.clicked.connect(self.clear)
        header_layout.addWidget(self._close_all_btn)

        layout.addWidget(header_container)

        # Vertical splitter: code editor (top) / info hub (bottom)
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setHandleWidth(3)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {BORDER}; }}"
        )

        from aura.gui.code_editor_pane import CodeEditorPane
        from aura.gui.info_hub_pane import InfoHubPane
        from aura.gui.terminal_window import TerminalWindow

        self._code_editor = CodeEditorPane(self._splitter)
        self._info_hub = InfoHubPane(self._splitter)
        self._code_editor.setMinimumHeight(96)
        self._info_hub.setMinimumHeight(48)
        self._code_editor.focused_action_requested.connect(
            self.focused_action_requested.emit
        )
        self._info_hub.stop_execution_requested.connect(self.stop_execution_requested.emit)

        self._splitter.addWidget(self._code_editor)
        self._splitter.addWidget(self._info_hub)

        # Let the terminal/log pane participate in vertical resizing instead of
        # being treated as a fixed-height footer.
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([560, 300])
        # Restore saved vertical splitter sizes if valid (len 2, sum > 0, each >= 40)
        if vertical_splitter_sizes is not None and len(vertical_splitter_sizes) == 2 and sum(vertical_splitter_sizes) > 0 and all(v >= 40 for v in vertical_splitter_sizes):
            self._splitter.setSizes(vertical_splitter_sizes)

        # Outer horizontal splitter: file tree (left) | code/log (right)
        self._outer_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._outer_splitter.setHandleWidth(3)
        self._outer_splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {BORDER}; }}"
        )

        # File tree panel (left side)
        self._file_tree_panel = QWidget()
        file_tree_layout = QVBoxLayout(self._file_tree_panel)
        file_tree_layout.setContentsMargins(0, 0, 0, 0)
        file_tree_layout.setSpacing(0)

        self._tree = WorkspaceTree(None)
        file_tree_layout.addWidget(self._tree, 1)

        # Right side: existing vertical splitter (code editor / info hub)
        self._outer_splitter.addWidget(self._file_tree_panel)
        self._outer_splitter.addWidget(self._splitter)

        self._outer_splitter.setStretchFactor(0, 0)
        self._outer_splitter.setStretchFactor(1, 1)
        self._outer_splitter.setSizes([240, 800])
        # Restore saved outer splitter sizes if valid (len 2, sum > 0, each >= 40)
        if outer_splitter_sizes is not None and len(outer_splitter_sizes) == 2 and sum(outer_splitter_sizes) > 0 and all(v >= 40 for v in outer_splitter_sizes):
            self._outer_splitter.setSizes(outer_splitter_sizes)

        # Stacked widget: index 0 = workspace view; Chain Editor uses a dynamic index
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._outer_splitter)  # index 0
        self._chain_editor: QWidget | None = None
        self._chain_editor_index: int | None = None

        layout.addWidget(self._stack, 1)

        # Floating terminal window. It is intentionally not added to this
        # layout, so terminal output never consumes execution/workspace space.
        self._terminal_window = TerminalWindow(
            self.window(),
            initial_geometry=terminal_window_geometry,
        )

        # Authoritative applied-write projection into the workspace editor.
        self._file_edit_projection = FileEditProjection(self._code_editor)
        self._workspace_root: Path | None = None

        # Active drone run card (shown below the stack widget)
        self._run_cards: dict[str, QWidget] = {}
        self._run_cards_host = QWidget(self)
        self._run_cards_layout = QVBoxLayout(self._run_cards_host)
        self._run_cards_layout.setContentsMargins(10, 6, 10, 10)
        self._run_cards_layout.setSpacing(8)
        self._run_cards_host.hide()
        layout.addWidget(self._run_cards_host)

        # Aura wrapper reference for atmospheric synchronization
        self._aura_wrapper: AuraWidget | None = None

    def set_chain_editor(self, chain_editor: QWidget) -> None:
        """Add or replace the chain editor at a dynamic stack index."""
        if self._chain_editor is not None:
            self._stack.removeWidget(self._chain_editor)
            self._chain_editor.deleteLater()
        self._chain_editor = chain_editor
        self._chain_editor_index = self._stack.addWidget(chain_editor)

    def toggle_chain_editor(self) -> None:
        """Switch the stacked widget to the chain editor view."""
        if self._chain_editor is not None and self._stack.currentIndex() != self._chain_editor_index:
            self._stack.setCurrentIndex(self._chain_editor_index)
            self.set_workspace_header("WORKFLOW EDITOR", show_close_all=False)

    def hide_chain_editor(self) -> None:
        """Switch back to workspace from chain editor."""
        if self._stack.currentIndex() == self._chain_editor_index:
            self.switch_to_workspace()

    def set_workspace_header(self, text: str, show_close_all: bool = True) -> None:
        """Update the header label and visibility of Close All button."""
        self._header_label.setText(text)
        self._close_all_btn.setVisible(show_close_all)

    def switch_to_workspace(self) -> None:
        """Switch the stacked widget to the normal workspace view (index 0)."""
        if self._stack.currentIndex() != 0:
            self._stack.setCurrentIndex(0)
            self.set_workspace_header("WORKSPACE", show_close_all=True)

    def is_chain_editor_open(self) -> bool:
        """Return True if the chain editor is currently displayed in the stack."""
        return self._chain_editor is not None and self._stack.currentIndex() == self._chain_editor_index

    def refresh_drone_bay(self) -> None:
        if hasattr(self, '_drone_bay') and self._drone_bay is not None and hasattr(self._drone_bay, 'refresh'):
            self._drone_bay.refresh()

    def set_aura_wrapper(self, wrapper: AuraWidget) -> None:
        self._aura_wrapper = wrapper

    def set_glow_state(self, state: str) -> None:
        if self._aura_wrapper:
            self._aura_wrapper.set_glow_state(state)

    def set_active_run_card(self, card: QWidget) -> None:
        """Insert a run card into the playground layout (below the stack)."""
        self.clear_active_run_card()
        self.add_run_card("__active__", card)

    def clear_active_run_card(self) -> None:
        """Remove the run card from the layout and destroy it."""
        self.clear_run_cards()

    def add_run_card(self, run_id: str, card: QWidget) -> None:
        """Insert or replace one Drone run/receipt card."""
        self.remove_run_card(run_id)
        self._run_cards[run_id] = card
        self._run_cards_layout.addWidget(card)
        self._run_cards_host.show()
        card.show()

    def remove_run_card(self, run_id: str) -> None:
        card = self._run_cards.pop(run_id, None)
        if card is None:
            return
        self._run_cards_layout.removeWidget(card)
        card.deleteLater()
        if not self._run_cards:
            self._run_cards_host.hide()

    def clear_run_cards(self) -> None:
        for run_id in list(self._run_cards):
            self.remove_run_card(run_id)

    def focus_run_card(self, run_id: str) -> None:
        card = self._run_cards.get(run_id)
        if card is not None:
            self.switch_to_workspace()
            card.setFocus(Qt.FocusReason.OtherFocusReason)

    def stop_aura(self) -> None:
        if self._aura_wrapper:
            self._aura_wrapper.stop_aura()

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root
        self._code_editor.set_workspace_root(root)
        self._tree.set_root(root)

    def file_tree(self) -> WorkspaceTree:
        return self._tree

    def set_read_only_mode(self, enabled: bool) -> None:
        self._code_editor.set_read_only_mode(enabled)

    def open_file(self, path: Path) -> None:
        self._code_editor.open_file(path)

    def terminal_window(self):
        return self._terminal_window

    def toggle_terminal_window(self) -> None:
        self._terminal_window.toggle()

    def is_terminal_window_open(self) -> bool:
        return self._terminal_window.is_open()

    # Public API (backward-compatible with execution_handler.py)

    def begin_assistant(self):
        """Prepare the workspace for a new run while retaining terminal history."""
        _log.info(
            "DIAGNOSTIC AuraPlayground.begin_assistant called — clearing code tabs and log"
        )
        self._code_editor.close_execution_tabs()
        self._info_hub.clear_log()

    def append_tool_args(self, execution_tool_id: str, fragment: str) -> None:
        """No-op: partial tool-call JSON never drives the workspace editor.

        Kept as a stable forwarding target for ExecutionToolEventRouter; raw
        argument fragments belong to generic argument/log UI (e.g. the chat
        transcript's own tool-call display), never to this authoritative
        editor pane.
        """

    def mark_execution_error(self) -> None:
        """Set the pane's truthful terminal status to Error mid-stream.

        Used for an API/harness error that interrupts a run before it
        reaches the normal finish/cancel path — the tabs and checklist are
        left untouched since the run's own evidence didn't change.
        """
        self._info_hub.set_terminal_status(False, "harness_error")

    def set_tool_result(self, execution_tool_id: str, ok: bool, result: str):
        # Finalize terminal window if this was a terminal tool.
        exit_code = 0
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                exit_code = parsed.get("exit_code", 0)
        except Exception:
            pass
        self._terminal_window.set_result(execution_tool_id, exit_code)

    def update_task_checklist(self, items: list[dict[str, str]], tool_call_id: str | None = None) -> None:
        """Render the latest Task Checklist snapshot in the info hub."""
        self._info_hub.update_task_checklist(items)

    def handle_file_edit_lifecycle(
        self,
        tool_call_id: str,
        tool_name: str,
        phase: str,
        changes: list[dict],
        reason: str,
    ) -> None:
        """Route one authoritative file-edit lifecycle phase to the editor.

        Only ``applied`` ever changes editor content, and only with content
        the write pipeline already confirmed landed on disk. The workspace
        tree also gets a nudge to refresh on structural changes (create /
        delete), since Aura may have written outside the currently expanded
        directories.
        """
        self._file_edit_projection.handle_lifecycle_event(
            tool_call_id, tool_name, phase, changes, reason
        )
        if phase == "applied" and any(
            change.get("action") in ("create", "delete") for change in changes
        ):
            self._tree.refresh()

    def handle_workspace_reconcile(self, tool_call_id: str) -> None:
        """Re-sync open editor tabs and the workspace tree after a shell command.

        A submitted shell command may have mutated the workspace opaquely
        (formatters, generators, builds, git) — this re-reads every currently
        open editor path from disk and refreshes the tree, without claiming
        any per-file "applied" lifecycle for a command whose file effects are
        unproven.
        """
        self._file_edit_projection.reconcile_workspace()
        self._tree.refresh()

    def start_terminal_process(self, process_id: str, command: str) -> None:
        self._terminal_window.set_command(process_id, command)

    def start_terminal_command(
        self,
        tool_call_id: str,
        command: str,
        starting_cwd: str = "",
    ) -> None:
        """Present a card only after the command was submitted for execution."""
        self._terminal_window.set_command(tool_call_id, command)

    def append_terminal_output(self, execution_tool_id: str, text: str) -> None:
        self._terminal_window.append_output(execution_tool_id, text)

    def finish_terminal_process(self, process_id: str, exit_code: int) -> None:
        self._terminal_window.set_result(process_id, exit_code)

    def execution_finished(self, ok: bool, summary: str, status: str | None = None) -> None:
        self._code_editor.close_all_tabs()
        self._info_hub.set_terminal_status(ok, status)
        self._info_hub.set_execution_running(False)

    def execution_cancelled(self):
        self._code_editor.close_all_tabs()
        self._info_hub.set_terminal_status(False, "cancelled")
        self._info_hub.set_execution_running(False)

    def set_execution_running(self, running: bool):
        self._info_hub.set_execution_running(running)

    def clear(self):
        _log.info("DIAGNOSTIC AuraPlayground.clear called — full workspace reset")
        self._code_editor.close_all_tabs()
        self._info_hub.clear()
        self._terminal_window.clear()

    def add_mermaid_artifact(self, code: str):
        pass

    def splitter_sizes(self) -> tuple[list[int], list[int]]:
        """Return current (outer_splitter_sizes, vertical_splitter_sizes)."""
        return (list(self._outer_splitter.sizes()), list(self._splitter.sizes()))

