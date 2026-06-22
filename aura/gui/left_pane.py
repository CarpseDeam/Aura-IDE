from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
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
from aura.drones.store import _global_drones_root
from aura.gui.theme import ACCENT, BG_ALT, BG_RAISED, BORDER, FG_DIM, FG_MUTED, LABEL_PROJECTS, LABEL_THREAD
from aura.projects.store import ProjectStore
from aura.providers.registry import provider_registry


class _ToggleToolButton(QToolButton):
    def mousePressEvent(self, event) -> None:
        event.accept()
        super().mousePressEvent(event)

class _ProjectRow(QFrame):
    clicked = Signal(Path)
    collapse_toggled = Signal(str)

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

        self.name_label = QLabel(project.name)
        self.name_label.setStyleSheet(
            f"color: {LABEL_PROJECTS if is_active else FG_DIM}; "
            f"font-weight: {'bold' if is_active else 'normal'};"
        )
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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.project.root_path)
            event.accept()
        else:
            super().mousePressEvent(event)

class _ThreadRow(QFrame):
    clicked = Signal(Path)

    def __init__(self, thread, parent=None) -> None:
        super().__init__(parent)
        self.thread = thread
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 8, 0)
        layout.setSpacing(4)

        self.title_label = QLabel(thread.title)
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

class _DroneRow(QFrame):
    clicked = Signal(Path)

    def __init__(self, folder: Path, name: str, is_active: bool, parent=None) -> None:
        super().__init__(parent)
        self.folder = folder
        self.is_active = is_active
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(4)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet(
            f"color: {LABEL_PROJECTS if is_active else FG_DIM}; "
            f"font-weight: {'bold' if is_active else 'normal'};"
        )
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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.folder)
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

        title = QLabel("Workspace")
        title.setObjectName("paneTitleWorkspace")
        layout.addWidget(title)

        self._workspace_label = QLabel("")
        self._workspace_label.setObjectName("workspaceLabel")
        self._workspace_label.setWordWrap(True)
        layout.addWidget(self._workspace_label)

        change_btn = QPushButton("Change Root...")
        change_btn.clicked.connect(self.change_root_requested.emit)
        change_row = QHBoxLayout()
        change_row.setContentsMargins(8, 0, 8, 6)
        change_row.addWidget(change_btn)
        layout.addLayout(change_row)

        # --- Projects section ---
        _projects_block = QWidget()
        _projects_block.setMinimumHeight(100)
        _projects_block_layout = QVBoxLayout(_projects_block)
        _projects_block_layout.setContentsMargins(0, 0, 0, 0)
        _projects_block_layout.setSpacing(4)

        projects_title = QLabel("Projects")
        projects_title.setObjectName("paneTitleProjects")
        _projects_block_layout.addWidget(projects_title)

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

        # --- Drones section ---
        _drones_block = QWidget()
        _drones_block.setMinimumHeight(100)
        _drones_block_layout = QVBoxLayout(_drones_block)
        _drones_block_layout.setContentsMargins(0, 0, 0, 0)
        _drones_block_layout.setSpacing(4)

        drones_title = QLabel("Drones")
        drones_title.setObjectName("paneTitleDrones")
        _drones_block_layout.addWidget(drones_title)

        new_drone_row = QHBoxLayout()
        new_drone_row.setContentsMargins(8, 0, 8, 6)
        self._new_drone_btn = QPushButton("＋ New Drone")
        self._new_drone_btn.clicked.connect(self.new_drone_requested.emit)
        new_drone_row.addWidget(self._new_drone_btn)
        _drones_block_layout.addLayout(new_drone_row)

        self._drones_scroll = QScrollArea()
        self._drones_scroll.setWidgetResizable(True)
        self._drones_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._drones_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._drones_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._drones_container = QWidget()
        self._drones_layout = QVBoxLayout(self._drones_container)
        self._drones_layout.setContentsMargins(0, 0, 0, 0)
        self._drones_layout.setSpacing(2)

        self._drones_scroll.setWidget(self._drones_container)
        _drones_block_layout.addWidget(self._drones_scroll, 1)

        # --- Splitter ---
        self._section_splitter = QSplitter(Qt.Orientation.Vertical)
        self._section_splitter.addWidget(_projects_block)
        self._section_splitter.addWidget(_drones_block)
        self._section_splitter.setCollapsible(0, False)
        self._section_splitter.setCollapsible(1, False)
        self._section_splitter.setSizes([300, 200])
        layout.addWidget(self._section_splitter, 1)

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
        self._planner_model_combo = QComboBox()
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
        self._planner_thinking_combo = QComboBox()
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
        self._worker_model_combo = QComboBox()
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
        self._worker_thinking_combo = QComboBox()
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

    def refresh_projects(self, workspace_root: Path | None, *, schedule_backfill: bool = False) -> None:
        """Update the projects list and show threads for the active project."""
        if workspace_root != self._last_workspace_root:
            self._show_all_active_threads = False
            self._last_workspace_root = workspace_root
            self._project_collapsed = {}

        self._clear_projects_layout()

        # Ensure the current workspace is registered as a project before listing
        if workspace_root is not None:
            try:
                # Use a single ProjectStore instance for efficiency
                store = ProjectStore()
                store.create_or_update_project(workspace_root)
            except Exception:
                logging.warning("Failed to register workspace as project")
                self._projects_layout.addStretch(1)
                return
        else:
            store = ProjectStore()

        try:
            projects = store.list_projects()
        except Exception:
            logging.warning("Failed to list projects")
            self._projects_layout.addStretch(1)
            return

        for project in projects:
            is_active = (workspace_root is not None and project.root_path.resolve() == workspace_root.resolve())

            row = _ProjectRow(project, is_active, parent=self._projects_container)
            row.clicked.connect(self.project_selected.emit)
            row.collapse_toggled.connect(self._on_project_collapse_toggled)
            row.set_collapsed(self._project_collapsed.get(project.id, False))
            self._projects_layout.addWidget(row)

            if is_active and not self._project_collapsed.get(project.id, False):
                try:
                    threads = store.list_threads(project, include_archived=False)
                except Exception:
                    logging.warning("Failed to list threads")
                    threads = []

                INITIAL_VISIBLE_THREADS = 10
                visible_threads = threads
                has_more = False

                if len(threads) > INITIAL_VISIBLE_THREADS and not self._show_all_active_threads:
                    visible_threads = threads[:INITIAL_VISIBLE_THREADS]
                    has_more = True

                for t in visible_threads:
                    t_row = _ThreadRow(t, parent=self._projects_container)
                    t_row.clicked.connect(self.thread_selected.emit)
                    self._projects_layout.addWidget(t_row)

                if has_more:
                    more_row = _ShowMoreRow(parent=self._projects_container)
                    more_row.clicked.connect(self._on_show_more_clicked)
                    self._projects_layout.addWidget(more_row)

        self._projects_layout.addStretch(1)

        if schedule_backfill:
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

    def refresh_drones(self, active_root: Path | None) -> None:
        from aura.drones.store import DroneStore

        while self._drones_layout.count():
            item = self._drones_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        ws_root = self._last_workspace_root
        if ws_root is None:
            return

        try:
            entries = DroneStore.list_drone_folders(ws_root)
        except Exception:
            logging.warning("Failed to list drone folders")
            self._drones_layout.addStretch(1)
            return

        global_root = _global_drones_root(ws_root)
        active_resolved = active_root.resolve() if active_root is not None else None

        for entry in entries:
            folder = global_root / entry.id
            is_active = (active_resolved is not None and folder.resolve() == active_resolved)
            row = _DroneRow(folder, entry.name, is_active, parent=self._drones_container)
            row.clicked.connect(self.drone_selected.emit)
            self._drones_layout.addWidget(row)

        self._drones_layout.addStretch(1)

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