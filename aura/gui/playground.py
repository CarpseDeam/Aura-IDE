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

from aura.gui.controllers import ToolStreamController
from aura.gui.dispatch_todo_rail import DispatchTodoRail
from aura.gui.theme import BORDER
from aura.gui.widgets.aura_glow import AuraWidget
from aura.gui.workspace_tree import WorkspaceTree


class AuraPlayground(QWidget):
    """Right-side workspace panel with code editor (top), info hub (middle),
    and worker log.

    Uses a vertical QSplitter to divide the space between a tabbed code editor
    pane and a tabbed info hub pane (Worker Log). Terminal output is routed to
    a floating TerminalWindow so it does not participate in this layout.
    """

    focused_action_requested = Signal(str)
    stop_worker_requested = Signal()

    def __init__(self, parent=None, terminal_window_geometry: str = ""):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Flat QVBoxLayout — no outer HBox or _content_widget wrapper
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header_container = QWidget(self)
        header_layout = QHBoxLayout(header_container)
        header_layout.setContentsMargins(12, 8, 12, 4)
        header_layout.setSpacing(8)

        self._header_label = QLabel("WORKSPACE", self)
        self._header_label.setObjectName("paneTitleWorkspace")
        header_layout.addWidget(self._header_label)

        header_layout.addStretch(1)

        self._close_all_btn = QToolButton(self)
        self._close_all_btn.setText("Close All")
        self._close_all_btn.setObjectName("closeAllBtn")
        self._close_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self._info_hub.stop_worker_requested.connect(self.stop_worker_requested.emit)

        self._splitter.addWidget(self._code_editor)
        self._splitter.addWidget(self._info_hub)

        # Let the terminal/log pane participate in vertical resizing instead of
        # being treated as a fixed-height footer.
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([560, 300])

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

        files_label = QLabel("FILES")
        files_label.setObjectName("paneTitleFiles")
        file_tree_layout.addWidget(files_label)

        self._tree = WorkspaceTree(None)
        file_tree_layout.addWidget(self._tree, 1)

        # Right side: existing vertical splitter (code editor / info hub)
        self._outer_splitter.addWidget(self._file_tree_panel)
        self._outer_splitter.addWidget(self._splitter)

        self._outer_splitter.setStretchFactor(0, 0)
        self._outer_splitter.setStretchFactor(1, 1)
        self._outer_splitter.setSizes([240, 800])

        # Stacked widget: index 0 = workspace view; Chain Editor uses a dynamic index
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._outer_splitter)  # index 0
        self._chain_editor: QWidget | None = None
        self._chain_editor_index: int | None = None

        layout.addWidget(self._stack, 1)

        # Floating terminal window. It is intentionally not added to this
        # layout, so terminal output never consumes worker/workspace space.
        self._terminal_window = TerminalWindow(
            self.window(),
            initial_geometry=terminal_window_geometry,
        )

        # Tool stream controllers keyed by worker_tool_id
        self._controllers: dict[str, ToolStreamController] = {}
        self._controller_parents: dict[str, str] = {}
        self._suppressed_worker_tool_ids: set[str] = set()
        self._worker_code_paths: dict[str, str] = {}
        self._worker_code_tool_names: dict[str, str] = {}
        self._pending_worker_code_content: dict[str, str] = {}
        self._workspace_root: Path | None = None
        self._dispatch_rail = DispatchTodoRail()
        self._canonical_active: set[str] = set()

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

    # Public API (backward-compatible with worker_handler.py)

    def begin_assistant(self):
        """Reset the workspace for a new assistant run (log text, code tabs, terminal).

        Does NOT clear the TODO rail — the bridge owns TODO state during dispatch.
        """
        self._code_editor.close_worker_tabs()
        self._info_hub.clear_log()
        self._terminal_window.clear()
        self._controllers.clear()
        self._controller_parents.clear()
        self._suppressed_worker_tool_ids.clear()
        self._worker_code_paths.clear()
        self._worker_code_tool_names.clear()
        self._pending_worker_code_content.clear()

    def begin_canonical_dispatch_tracking(self, tool_call_id: str, steps: list) -> None:
        """Mark a tool_call_id as canonical for suppression purposes.

        The visible dispatch TODO rail is owned by DispatchSession, which
        emits canonical snapshots (pending → active → done) via
        dispatchTodoListUpdated. This method only ensures Worker-local
        update_todo_list tool calls and log boundaries are suppressed
        during the dispatch campaign.
        """
        _log.debug(
            "begin_canonical_dispatch_tracking tool_call_id=%s step_count=%d",
            tool_call_id, len(steps),
        )
        self._canonical_active.add(tool_call_id)

    def render_dispatch_todo_list(self, tool_call_id: str) -> None:
        """Replay the last cached canonical TODO snapshot for a tool_call_id.

        Only replays when the cache already holds a snapshot. An empty cache
        means the first ``dispatchTodoListUpdated`` signal has not arrived yet
        and replaying ``[]`` would clear the visible TODO rail. The arriving
        signal will seed the cache and render the correct tasks.

        The visible execution checklist remains visible after completion —
        ``finish_todo_list`` replays the last canonical snapshot rather than
        clearing it.
        """
        snapshot = self._dispatch_rail.replay(tool_call_id)
        if not snapshot:
            _log.debug(
                "render_dispatch_todo_list tool_call_id=%s cache empty — skipping replay",
                tool_call_id,
            )
            return
        _log.debug(
            "render_dispatch_todo_list tool_call_id=%s replaying %d tasks statuses=%s",
            tool_call_id, len(snapshot),
            [t.get("status", "?") for t in snapshot if isinstance(t, dict)],
        )
        self._info_hub.update_todo_list(snapshot)

    def append_reasoning(self, text: str):
        self._info_hub.append_reasoning(text)

    def append_content(self, text: str):
        self._info_hub.append_content(text)

    def add_tool_call(self, worker_tool_id: str, name: str, parent_tool_id: str | None = None):
        is_canonical = parent_tool_id in self._canonical_active if parent_tool_id else False

        # During canonical dispatch, suppress update_todo_list entirely.
        # The canonical execution checklist owns the TODO rail; Worker TODO
        # calls are never shown, so creating a controller is pure noise.
        if name == "update_todo_list" and is_canonical:
            self._suppressed_worker_tool_ids.add(worker_tool_id)
            return

        # During canonical dispatch, keep the Worker Log calm — tool call /
        # result boundaries are internal gear teeth the user does not need.
        if not is_canonical:
            self._info_hub.flush_worker_log()
            self._info_hub.mark_worker_log_boundary()

        c = ToolStreamController(name, self)
        self._controllers[worker_tool_id] = c
        self._controller_parents[worker_tool_id] = parent_tool_id or ""

        if name == "update_todo_list" and parent_tool_id not in self._canonical_active:
            c.todo_updated.connect(
                lambda tasks, tid=parent_tool_id: self.update_todo_list(tasks, tid)
            )

        if name in ("write_file", "apply_edit_transaction", "edit_file", "edit_symbol"):
            self._worker_code_tool_names[worker_tool_id] = name
            c.path_resolved.connect(
                lambda path, tid=worker_tool_id: self._on_code_path_resolved(
                    tid, path
                )
            )
            c.content_updated.connect(
                lambda content, tid=worker_tool_id: self._on_code_content_updated(
                    tid, content
                )
            )

        if name == "run_terminal_command":
            c.command_resolved.connect(
                lambda cmd, tid=worker_tool_id: self._terminal_window.set_command(tid, cmd)
            )

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        if worker_tool_id in self._suppressed_worker_tool_ids:
            return
        controller = self._controllers.get(worker_tool_id)
        if controller is None:
            return
        controller.append_fragment(fragment)

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str):
        if worker_tool_id in self._suppressed_worker_tool_ids:
            self._suppressed_worker_tool_ids.discard(worker_tool_id)
            return

        # During canonical dispatch, suppress Worker Log boundary noise.
        # Tool results still drive code editor tabs and terminal output —
        # only the visual separators in the log are silenced.
        parent_tool_id = self._controller_parents.pop(worker_tool_id, "")
        is_canonical = parent_tool_id in self._canonical_active
        if not is_canonical:
            self._info_hub.flush_worker_log()
            self._info_hub.mark_worker_log_boundary()
        controller = self._controllers.pop(worker_tool_id, None)
        if controller is not None:
            controller.finalize(ok, result)

        # Finalize code editor tab if this was a file tool
        self._code_editor.finalize_tab(worker_tool_id)
        self._worker_code_paths.pop(worker_tool_id, None)
        self._worker_code_tool_names.pop(worker_tool_id, None)
        self._pending_worker_code_content.pop(worker_tool_id, None)

        # Finalize terminal window if this was a terminal tool.
        exit_code = 0
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                exit_code = parsed.get("exit_code", 0)
        except Exception:
            pass
        self._terminal_window.set_result(worker_tool_id, exit_code)

    def update_todo_list(self, tasks: list, tool_call_id: str | None = None):
        """Render a TODO snapshot and cache it for replay.

        The single entry point for all TODO snapshot rendering. Accepts both
        canonical ``DispatchSession`` snapshots (via ``dispatchTodoListUpdated``)
        and Worker-local TODO updates (the latter only when no canonical dispatch
        is active — see ``WorkerEventHandler._on_worker_todo_list_updated``).

        The ``TodoListWidget`` is a pure renderer: snapshot in, rows painted.
        No status inference, repair, collapse, or canonical state mutation
        happens here.
        """
        snapshot = self._dispatch_rail.set(tool_call_id, tasks)
        _log.debug(
            "update_todo_list tool_call_id=%s task_count=%d statuses=%s",
            tool_call_id, len(snapshot),
            [t.get("status", "?") for t in snapshot if isinstance(t, dict)],
        )
        self._info_hub.update_todo_list(snapshot)

    def finish_todo_list(self, tool_call_id: str, *, ok: bool, needs_followup: bool) -> None:
        """Replay the last canonical snapshot and clear canonical tracking.

        The visible execution checklist remains visible after completion.
        Does not invent statuses — the last DispatchSession snapshot is the
        final truth for the visible TODO rail.
        """
        self._canonical_active.discard(tool_call_id)
        self._info_hub.update_todo_list(self._dispatch_rail.replay(tool_call_id))

    def update_activity(self, entries: list, tool_call_id: str | None = None) -> None:
        """Render a Worker Activity snapshot in the info hub."""
        self._info_hub.update_activity(entries)

    def _on_code_path_resolved(self, worker_tool_id: str, path: str) -> None:
        self._worker_code_paths[worker_tool_id] = path
        self._code_editor.open_or_focus_tab(worker_tool_id, path)
        tool_name = self._worker_code_tool_names.get(worker_tool_id)
        if tool_name in ("apply_edit_transaction", "edit_file", "edit_symbol"):
            current_content = self._read_workspace_text(path)
            if current_content is not None:
                self._code_editor.set_content(worker_tool_id, current_content)
        pending_content = self._pending_worker_code_content.pop(worker_tool_id, None)
        if pending_content is not None and tool_name not in ("apply_edit_transaction", "edit_file", "edit_symbol"):
            self._code_editor.stream_content(worker_tool_id, pending_content)

    def _on_code_content_updated(self, worker_tool_id: str, content: str) -> None:
        tool_name = self._worker_code_tool_names.get(worker_tool_id)
        if tool_name in ("apply_edit_transaction", "edit_file", "edit_symbol"):
            return
        if worker_tool_id not in self._worker_code_paths:
            self._pending_worker_code_content[worker_tool_id] = content
            return
        self._code_editor.stream_content(worker_tool_id, content)

    def show_code_diff(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
    ) -> None:
        path = self._worker_code_paths.get(worker_tool_id, rel_path)
        self._worker_code_paths[worker_tool_id] = path
        self._code_editor.open_or_focus_tab(worker_tool_id, path)
        if decision in ("approve", "approve_all"):
            self._code_editor.animate_content_transition(worker_tool_id, old, new)
        else:
            self._code_editor.set_content(worker_tool_id, old)

    def _read_workspace_text(self, path: str) -> str | None:
        candidate = Path(path)
        if not candidate.is_absolute() and self._workspace_root is not None:
            candidate = self._workspace_root / candidate
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        self._info_hub.add_diff_card(rel_path, old, new, decision, is_new_file)

    def add_error(self, message: str) -> None:
        self._info_hub.add_error(message)

    def start_terminal_process(self, process_id: str, command: str) -> None:
        self._terminal_window.set_command(process_id, command)

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        self._terminal_window.append_output(worker_tool_id, text)

    def finish_terminal_process(self, process_id: str, exit_code: int) -> None:
        self._terminal_window.set_result(process_id, exit_code)

    def worker_finished(self, ok: bool, summary: str, needs_followup: bool = False, status: str | None = None) -> None:
        self._code_editor.close_all_tabs()
        self._controllers.clear()
        self._controller_parents.clear()
        self._suppressed_worker_tool_ids.clear()
        self._worker_code_paths.clear()
        self._worker_code_tool_names.clear()
        self._pending_worker_code_content.clear()
        self._info_hub.show_final_summary(ok, summary, needs_followup=needs_followup, status=status)
        self._info_hub.set_worker_running(False)

    def worker_cancelled(self):
        self._code_editor.close_all_tabs()
        self._controllers.clear()
        self._controller_parents.clear()
        self._suppressed_worker_tool_ids.clear()
        self._worker_code_paths.clear()
        self._worker_code_tool_names.clear()
        self._pending_worker_code_content.clear()
        self._info_hub.show_final_summary(False, "Worker stopped by user.", status="cancelled")
        self._info_hub.set_worker_running(False)

    def set_worker_running(self, running: bool):
        self._info_hub.set_worker_running(running)

    def clear(self):
        self._code_editor.close_all_tabs()
        self._info_hub.clear()
        self._terminal_window.clear()
        self._controllers.clear()
        self._controller_parents.clear()
        self._suppressed_worker_tool_ids.clear()
        self._worker_code_paths.clear()
        self._worker_code_tool_names.clear()
        self._pending_worker_code_content.clear()
        self._dispatch_rail.clear()
        self._canonical_active.clear()

    def add_mermaid_artifact(self, code: str):
        pass
