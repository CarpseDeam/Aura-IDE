from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    DEFAULT_PLANNER_THINKING,
    DEFAULT_WORKER_THINKING,
    ModelInfo,
    ProviderId,
    ThinkingMode,
)
from aura.gui.theme import ACCENT, BG_ALT, BG_RAISED, BORDER, FG_DIM, FG_MUTED, LABEL_PROJECTS, LABEL_THREAD
from aura.gui.widgets.no_wheel_combo import NoWheelComboBox
from aura.projects.store import ProjectStore
from aura.providers.registry import provider_registry


class _ToggleToolButton(QToolButton):
    def mousePressEvent(self, event) -> None:
        event.accept()
        super().mousePressEvent(event)

class _ElidedLabel(QLabel):
    """A QLabel that elides text with an ellipsis when the label is too narrow."""
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(1)
        self.setWordWrap(False)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._update_elision()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        w = self.width()
        if w < 1:
            return
        metrics = self.fontMetrics()
        elided = metrics.elidedText(self._full_text, Qt.ElideRight, w)
        self.setText(elided)

class _ProjectRow(QFrame):
    clicked = Signal(Path)
    collapse_toggled = Signal(str)
    rename_requested = Signal(str, str)  # (project_id, new_name)

    def __init__(self, project, is_active: bool, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.is_active = is_active
        self._collapsed = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(4)

        self.toggle_btn = _ToggleToolButton(self)
        self.toggle_btn.setObjectName("sectionToggle")
        self.toggle_btn.setFixedWidth(16)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(self._on_toggle_clicked)
        layout.addWidget(self.toggle_btn)

        self.name_label = _ElidedLabel(project.name)
        self.name_label.setStyleSheet(
            f"color: {LABEL_PROJECTS if is_active else FG_DIM}; "
            f"font-weight: {'bold' if is_active else 'normal'};"
        )
        self.name_label.setToolTip(f"{project.name}\n{project.root_path}")
        layout.addWidget(self.name_label, 1)

        border_left_style = f"3px solid {ACCENT}" if is_active else "3px solid transparent"
        bg_style = BG_ALT if is_active else "transparent"
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_style};
                border-left: {border_left_style};
            }}
            QFrame:hover {{
                background-color: {BG_RAISED};
            }}
        """)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        caret = "\u25b8" if collapsed else "\u25be"  # ▸ vs ▾
        self.toggle_btn.setText(caret)

    def _on_toggle_clicked(self) -> None:
        self.collapse_toggled.emit(self.project.id)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        rename_act = menu.addAction("Rename Project")
        chosen = menu.exec(event.globalPos())
        if chosen == rename_act:
            name, ok = QInputDialog.getText(
                self, "Rename Project", "New name:",
                text=self.project.name
            )
            if ok and name.strip():
                self.rename_requested.emit(self.project.id, name.strip())
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.project.root_path)
            event.accept()
        else:
            super().mousePressEvent(event)

class _ThreadRow(QFrame):
    clicked = Signal(Path)
    rename_requested = Signal(str, str, str)  # (project_id, thread_id, new_title)

    def __init__(self, thread, parent=None) -> None:
        super().__init__(parent)
        self.thread = thread
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 8, 0)
        layout.setSpacing(4)

        self.title_label = _ElidedLabel(thread.title)
        self.title_label.setObjectName("threadTitle")
        tooltip = thread.summary if thread.summary else thread.title
        self.title_label.setToolTip(tooltip)
        self.title_label.setStyleSheet(f"color: {LABEL_THREAD}; font-size: 12px;")
        layout.addWidget(self.title_label, 1)

        self.setStyleSheet(f"""
            QFrame {{
                background-color: transparent;
            }}
            QFrame:hover {{
                background-color: {BG_RAISED};
            }}
        """)

    def update_thread(self, thread) -> None:
        """Update row in place with new thread metadata.

        Only title, summary, and updated_at change — the row widget is not
        replaced.  Stored thread reference, label text, elision, and tooltip
        are all refreshed.
        """
        self.thread = thread
        self.title_label.set_full_text(thread.title)
        tooltip = thread.summary if thread.summary else thread.title
        self.title_label.setToolTip(tooltip)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        rename_act = menu.addAction("Rename Chat")
        chosen = menu.exec(event.globalPos())
        if chosen == rename_act:
            title, ok = QInputDialog.getText(
                self, "Rename Chat", "New title:",
                text=self.thread.title
            )
            if ok and title.strip():
                self.rename_requested.emit(self.thread.project_id, self.thread.id, title.strip())
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self.thread.conversation_path:
                self.clicked.emit(Path(self.thread.conversation_path))
            event.accept()
        else:
            super().mousePressEvent(event)

class _ShowMoreRow(QFrame):
    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 8, 0)
        layout.setSpacing(4)

        label = QLabel("Show more...")
        label.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px; font-style: italic;")
        layout.addWidget(label, 1)

        self.setStyleSheet(f"""
            QFrame {{
                background-color: transparent;
            }}
            QFrame:hover {{
                background-color: {BG_RAISED};
            }}
        """)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class LeftPane(QFrame):
    change_root_requested = Signal()
    project_selected = Signal(Path)
    thread_selected = Signal(Path)  # conversation_path
    new_project_requested = Signal()
    planner_model_changed = Signal(str)
    planner_thinking_changed = Signal(str)
    worker_model_changed = Signal(str)
    worker_thinking_changed = Signal(str)
    drone_selected = Signal(Path)
    new_drone_requested = Signal()

    def __init__(self, workspace_root: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("leftPane")
        self.setMinimumWidth(160)

        self._last_workspace_root = workspace_root
        self._show_all_active_threads = False
        self._project_collapsed: dict[str, bool] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)

        # --- Projects section ---
        _projects_block = QWidget()
        _projects_block.setMinimumHeight(100)
        _projects_block_layout = QVBoxLayout(_projects_block)
        _projects_block_layout.setContentsMargins(0, 0, 0, 0)
        _projects_block_layout.setSpacing(4)

        projects_title = QLabel("Projects")
        projects_title.setObjectName("paneTitleProjects")
        _projects_block_layout.addWidget(projects_title)

        self._workspace_label = QLabel("")
        self._workspace_label.setObjectName("workspaceLabel")
        self._workspace_label.setWordWrap(True)
        _projects_block_layout.addWidget(self._workspace_label)

        change_btn = QPushButton("Change Folder...")
        change_btn.clicked.connect(self.change_root_requested.emit)
        change_row = QHBoxLayout()
        change_row.setContentsMargins(8, 0, 8, 6)
        change_row.addWidget(change_btn)
        _projects_block_layout.addLayout(change_row)

        new_project_row = QHBoxLayout()
        new_project_row.setContentsMargins(8, 0, 8, 6)
        self._new_project_btn = QPushButton("＋ New Project")
        self._new_project_btn.clicked.connect(self.new_project_requested.emit)
        new_project_row.addWidget(self._new_project_btn)
        _projects_block_layout.addLayout(new_project_row)

        self._projects_scroll = QScrollArea()
        self._projects_scroll.setWidgetResizable(True)
        self._projects_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._projects_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._projects_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._projects_container = QWidget()
        self._projects_layout = QVBoxLayout(self._projects_container)
        self._projects_layout.setContentsMargins(0, 0, 0, 0)
        self._projects_layout.setSpacing(2)

        self._projects_scroll.setWidget(self._projects_container)
        _projects_block_layout.addWidget(self._projects_scroll, 1)

        layout.addWidget(_projects_block, 1)

        # --- Model Config section ---
        self._model_config_footer = QFrame()
        self._model_config_footer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        footer_layout = QVBoxLayout(self._model_config_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(4)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        footer_layout.addWidget(sep)

        model_label = QLabel("Model Configuration")
        model_label.setObjectName("paneTitleModel")
        footer_layout.addWidget(model_label)

        # Planner model
        planner_model_row = QHBoxLayout()
        planner_model_row.setSpacing(4)
        planner_model_label = QLabel("Planner:")
        planner_model_label.setStyleSheet(f"color: {FG_DIM};")
        planner_model_row.addWidget(planner_model_label)
        self._planner_model_combo = NoWheelComboBox()
        self._planner_model_combo.currentIndexChanged.connect(
            lambda: self.planner_model_changed.emit(self.current_planner_model())
        )
        planner_model_row.addWidget(self._planner_model_combo, 1)
        footer_layout.addLayout(planner_model_row)

        # Planner thinking
        planner_think_row = QHBoxLayout()
        planner_think_row.setSpacing(4)
        planner_think_label = QLabel("Thinking:")
        planner_think_label.setStyleSheet(f"color: {FG_DIM};")
        planner_think_row.addWidget(planner_think_label)
        self._planner_thinking_combo = NoWheelComboBox()
        self._planner_thinking_combo.addItem("Off", "off")
        self._planner_thinking_combo.addItem("High", "high")
        self._planner_thinking_combo.addItem("Max", "max")
        self._planner_thinking_combo.setCurrentIndex(["off", "high", "max"].index(DEFAULT_PLANNER_THINKING))
        self._planner_thinking_combo.currentIndexChanged.connect(
            lambda: self.planner_thinking_changed.emit(self.current_planner_thinking())
        )
        planner_think_row.addWidget(self._planner_thinking_combo, 1)
        footer_layout.addLayout(planner_think_row)

        # Worker model
        worker_model_row = QHBoxLayout()
        worker_model_row.setSpacing(4)
        self._worker_model_label = QLabel("Worker:")
        self._worker_model_label.setStyleSheet(f"color: {FG_DIM};")
        worker_model_row.addWidget(self._worker_model_label)
        self._worker_model_combo = NoWheelComboBox()
        self._worker_model_combo.currentIndexChanged.connect(
            lambda: self.worker_model_changed.emit(self.current_worker_model())
        )
        worker_model_row.addWidget(self._worker_model_combo, 1)
        footer_layout.addLayout(worker_model_row)

        # Worker thinking
        worker_think_row = QHBoxLayout()
        worker_think_row.setSpacing(4)
        self._worker_thinking_label = QLabel("Thinking:")
        self._worker_thinking_label.setStyleSheet(f"color: {FG_DIM};")
        worker_think_row.addWidget(self._worker_thinking_label)
        self._worker_thinking_combo = NoWheelComboBox()
        self._worker_thinking_combo.addItem("Off", "off")
        self._worker_thinking_combo.addItem("High", "high")
        self._worker_thinking_combo.addItem("Max", "max")
        self._worker_thinking_combo.setCurrentIndex(["off", "high", "max"].index(DEFAULT_WORKER_THINKING))
        self._worker_thinking_combo.currentIndexChanged.connect(
            lambda: self.worker_thinking_changed.emit(self.current_worker_thinking())
        )
        worker_think_row.addWidget(self._worker_thinking_combo, 1)
        footer_layout.addLayout(worker_think_row)

        layout.addWidget(self._model_config_footer)

        self.update_workspace_label(workspace_root)

    def update_workspace_label(self, root: Path | None) -> None:
        if root is None:
            self._workspace_label.setText("(none)")
            return
        self._workspace_label.setText(str(root))

    def populate_models(self, planner_provider: ProviderId, worker_provider: ProviderId) -> None:
        # Planner
        self._planner_model_combo.blockSignals(True)
        self._planner_model_combo.clear()
        for mid, info in _models_with_default(planner_provider).items():
            self._planner_model_combo.addItem(info.label, mid)
        self._planner_model_combo.blockSignals(False)

        # Worker
        self._worker_model_combo.blockSignals(True)
        self._worker_model_combo.clear()
        for mid, info in _models_with_default(worker_provider).items():
            self._worker_model_combo.addItem(info.label, mid)
        self._worker_model_combo.blockSignals(False)

    def current_planner_model(self) -> str:
        return self._planner_model_combo.currentData()

    def current_planner_thinking(self) -> ThinkingMode:
        return self._planner_thinking_combo.currentData()

    def current_worker_model(self) -> str:
        return self._worker_model_combo.currentData()

    def current_worker_thinking(self) -> ThinkingMode:
        return self._worker_thinking_combo.currentData()

    def set_planner_model(self, model: str) -> None:
        idx = self._planner_model_combo.findData(model)
        if idx >= 0:
            self._planner_model_combo.setCurrentIndex(idx)

    def set_planner_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._planner_thinking_combo.setCurrentIndex(keys.index(thinking))

    def set_worker_model(self, model: str) -> None:
        idx = self._worker_model_combo.findData(model)
        if idx >= 0:
            self._worker_model_combo.setCurrentIndex(idx)

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._worker_thinking_combo.setCurrentIndex(keys.index(thinking))

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._worker_model_label.setVisible(enabled)
        self._worker_model_combo.setVisible(enabled)
        self._worker_thinking_label.setVisible(enabled)
        self._worker_thinking_combo.setVisible(enabled)

    def _clear_projects_layout(self) -> None:
        while self._projects_layout.count():
            item = self._projects_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ------------------------------------------------------------------
    # Structural snapshot helpers for in-place-update optimisation
    # ------------------------------------------------------------------

    @staticmethod
    def _visible_thread_slice(
        threads: list, show_all: bool, *, limit: int = 10
    ) -> tuple[list, bool]:
        """Return ``(visible_threads, has_more)`` for the given thread list."""
        if len(threads) > limit and not show_all:
            return threads[:limit], True
        return list(threads), False

    def _capture_layout_snapshot(self) -> dict:
        """Walk the current ``_projects_layout`` widgets and return a
        lightweight structural snapshot suitable for comparison."""
        project_ids: list[str] = []
        collapsed: dict[str, bool] = {}
        visible_thread_ids: dict[str, list[str]] = {}
        has_more: dict[str, bool] = {}
        active_project_id: str | None = None

        current_project_id: str | None = None

        for i in range(self._projects_layout.count()):
            item = self._projects_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                # Spacer item — ignore.
                continue
            if isinstance(w, _ProjectRow):
                pid = w.project.id
                project_ids.append(pid)
                collapsed[pid] = w._collapsed
                current_project_id = pid
                if w.is_active:
                    active_project_id = pid
                visible_thread_ids[pid] = []
                has_more[pid] = False
            elif isinstance(w, _ThreadRow) and current_project_id is not None:
                visible_thread_ids[current_project_id].append(w.thread.id)
            elif isinstance(w, _ShowMoreRow) and current_project_id is not None:
                has_more[current_project_id] = True

        return {
            "workspace_root": self._last_workspace_root,
            "project_ids": project_ids,
            "active_project_id": active_project_id,
            "collapsed": collapsed,
            "show_all": self._show_all_active_threads,
            "visible_thread_ids": visible_thread_ids,
            "has_more": has_more,
        }

    def _compute_target_snapshot(
        self, workspace_root: Path | None, projects: list
    ) -> dict:
        """Compute what the structural snapshot *would* be from the given
        project list without touching the widget tree."""
        project_ids: list[str] = []
        collapsed: dict[str, bool] = {}
        visible_thread_ids: dict[str, list[str]] = {}
        has_more: dict[str, bool] = {}
        active_project_id: str | None = None

        store = ProjectStore()

        for project in projects:
            pid = project.id
            project_ids.append(pid)
            is_active = (
                workspace_root is not None
                and project.root_path.resolve() == workspace_root.resolve()
            )
            if is_active:
                active_project_id = pid

            col = self._project_collapsed.get(pid, False)
            collapsed[pid] = col

            if is_active and not col:
                try:
                    threads = store.list_threads(project, include_archived=False)
                except Exception:
                    threads = []
                vt, hm = self._visible_thread_slice(threads, self._show_all_active_threads)
                visible_thread_ids[pid] = [t.id for t in vt]
                has_more[pid] = hm
            else:
                visible_thread_ids[pid] = []
                has_more[pid] = False

        return {
            "workspace_root": workspace_root,
            "project_ids": project_ids,
            "active_project_id": active_project_id,
            "collapsed": collapsed,
            "show_all": self._show_all_active_threads,
            "visible_thread_ids": visible_thread_ids,
            "has_more": has_more,
        }

    @staticmethod
    def _snapshots_equal(old: dict, new: dict) -> bool:
        """Return True when *old* and *new* structural snapshots are identical."""
        if old["workspace_root"] != new["workspace_root"]:
            return False
        if old["project_ids"] != new["project_ids"]:
            return False
        if old["active_project_id"] != new["active_project_id"]:
            return False
        if old["show_all"] != new["show_all"]:
            return False
        for pid in old["project_ids"]:
            if old["collapsed"].get(pid) != new["collapsed"].get(pid):
                return False
            if old["visible_thread_ids"].get(pid) != new["visible_thread_ids"].get(pid):
                return False
            if old["has_more"].get(pid) != new["has_more"].get(pid):
                return False
        return True

    def _apply_in_place_thread_updates(self, threads_by_id: dict) -> None:
        """Walk the layout's ``_ThreadRow`` widgets and call ``update_thread``
        when a matching thread is found in *threads_by_id*."""
        for i in range(self._projects_layout.count()):
            item = self._projects_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if isinstance(w, _ThreadRow):
                tid = w.thread.id
                new_thread = threads_by_id.get(tid)
                if new_thread is not None:
                    w.update_thread(new_thread)

    def refresh_projects(self, workspace_root: Path | None, *, schedule_backfill: bool = False) -> None:
        """Update the projects list and show threads for the active project.

        When the project/thread *structure* is unchanged (same projects, same
        active project, same visible thread IDs in the same order), only
        thread-row metadata is updated in place — no widgets are destroyed or
        recreated.  A full rebuild is triggered whenever the structure
        actually changes (workspace root, project list, active project,
        collapsed state, or "show more" toggle).
        """
        # ---- workspace-root change resets transient state -----------------
        if workspace_root != self._last_workspace_root:
            self._show_all_active_threads = False
            self._last_workspace_root = workspace_root
            self._project_collapsed = {}

        # ---- resolve project list from store ------------------------------
        if workspace_root is not None:
            try:
                store = ProjectStore()
                store.create_or_update_project(workspace_root)
            except Exception:
                logging.warning("Failed to register workspace as project")
                self._clear_projects_layout()
                self._projects_layout.addStretch(1)
                return
        else:
            store = ProjectStore()

        try:
            projects = store.list_projects()
        except Exception:
            logging.warning("Failed to list projects")
            self._clear_projects_layout()
            self._projects_layout.addStretch(1)
            return

        # ---- structural snapshot comparison -------------------------------
        old_snapshot = self._capture_layout_snapshot()
        new_snapshot = self._compute_target_snapshot(workspace_root, projects)

        if old_snapshot["project_ids"] and self._snapshots_equal(old_snapshot, new_snapshot):
            # Structure unchanged — update thread rows in place only.
            active_pid = new_snapshot["active_project_id"]
            if active_pid is not None:
                try:
                    threads = store.list_threads(
                        store.load_project(active_pid), include_archived=False
                    )
                except Exception:
                    threads = []
                vt, _ = self._visible_thread_slice(threads, self._show_all_active_threads)
                threads_by_id = {t.id: t for t in vt}
                self._apply_in_place_thread_updates(threads_by_id)
            # Still honour backfill request (structure may need refresh
            # after backfill discovers new threads).
            if schedule_backfill:
                self._schedule_backfill(projects, store)
            return

        # ---- structure changed — full rebuild -----------------------------
        self._clear_projects_layout()

        INITIAL_VISIBLE_THREADS = 10

        for project in projects:
            is_active = (
                workspace_root is not None
                and project.root_path.resolve() == workspace_root.resolve()
            )

            row = _ProjectRow(project, is_active, parent=self._projects_container)
            row.clicked.connect(self.project_selected.emit)
            row.collapse_toggled.connect(self._on_project_collapse_toggled)
            row.rename_requested.connect(self._on_rename_project)
            row.set_collapsed(self._project_collapsed.get(project.id, False))
            self._projects_layout.addWidget(row)

            if is_active and not self._project_collapsed.get(project.id, False):
                try:
                    threads = store.list_threads(project, include_archived=False)
                except Exception:
                    logging.warning("Failed to list threads")
                    threads = []

                visible_threads, has_more = self._visible_thread_slice(
                    threads, self._show_all_active_threads, limit=INITIAL_VISIBLE_THREADS
                )

                for t in visible_threads:
                    t_row = _ThreadRow(t, parent=self._projects_container)
                    t_row.clicked.connect(self.thread_selected.emit)
                    t_row.rename_requested.connect(self._on_rename_thread)
                    self._projects_layout.addWidget(t_row)

                if has_more:
                    more_row = _ShowMoreRow(parent=self._projects_container)
                    more_row.clicked.connect(self._on_show_more_clicked)
                    self._projects_layout.addWidget(more_row)

        self._projects_layout.addStretch(1)

        if schedule_backfill:
            self._schedule_backfill(projects, store)

    def _schedule_backfill(self, projects, store: ProjectStore) -> None:
        def _do_backfill():
            for project in projects:
                try:
                    store.backfill_threads_from_conversations(project)
                except Exception:
                    logging.warning("Failed to backfill threads")
            # Refresh to show any newly discovered threads
            if self._last_workspace_root is not None:
                self.refresh_projects(self._last_workspace_root)

        QTimer.singleShot(0, _do_backfill)

    def _on_show_more_clicked(self) -> None:
        self._show_all_active_threads = True
        self.refresh_projects(self._last_workspace_root)

    @Slot(str)
    def _on_project_collapse_toggled(self, project_id: str) -> None:
        self._project_collapsed[project_id] = not self._project_collapsed.get(project_id, False)
        self.refresh_projects(self._last_workspace_root)

    @Slot(str, str)
    def _on_rename_project(self, project_id: str, new_name: str) -> None:
        store = ProjectStore()
        result = store.rename_project(project_id, new_name)
        if result is not None:
            self.refresh_projects(self._last_workspace_root)

    @Slot(str, str, str)
    def _on_rename_thread(self, project_id: str, thread_id: str, new_title: str) -> None:
        store = ProjectStore()
        project = store.load_project(project_id)
        if project is None:
            return
        result = store.rename_thread(project, thread_id, new_title)
        if result is not None:
            self.refresh_projects(self._last_workspace_root)

    def refresh_drones(self, active_root: Path | None) -> None:
        return

def _models_with_default(provider: ProviderId) -> dict[str, ModelInfo]:
    spec = provider_registry.get(provider)
    models = dict(spec.models)
    if spec.default_model not in models:
        models[spec.default_model] = ModelInfo(
            id=spec.default_model,
            label=spec.default_model.split("/")[-1].replace("-", " ").title(),
            **{chr(105)+chr(110)+chr(112)+chr(117)+chr(116)+"_per_m_usd": 0.0},
            output_per_m_usd=0.0,
            cache_hit_per_m_usd=0.0,
        )
    if provider == "deepseek":
        from aura.providers.catalog import DEFAULT_WORKER_MODEL

        if DEFAULT_WORKER_MODEL not in models:
            models[DEFAULT_WORKER_MODEL] = ModelInfo(
                id=DEFAULT_WORKER_MODEL,
                label=DEFAULT_WORKER_MODEL,
                **{chr(105)+chr(110)+chr(112)+chr(117)+chr(116)+"_per_m_usd": 0.0},
                output_per_m_usd=0.0,
                cache_hit_per_m_usd=0.0,
            )
    return models