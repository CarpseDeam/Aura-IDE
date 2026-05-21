from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from aura.config import (
    DEFAULT_PLANNER_THINKING,
    DEFAULT_WORKER_THINKING,
    ModelInfo,
    ProviderId,
    ThinkingMode,
)
from aura.providers.registry import provider_registry
from aura.gui.theme import BORDER, FG_DIM
from aura.gui.workspace_tree import WorkspaceTree
from aura.projects.store import ProjectStore


class LeftPane(QFrame):
    change_root_requested = Signal()
    planner_model_changed = Signal(str)
    planner_thinking_changed = Signal(str)
    worker_model_changed = Signal(str)
    worker_thinking_changed = Signal(str)
    thread_selected = Signal(Path)  # conversation_path

    def __init__(self, workspace_root: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("leftPane")
        self.setMinimumWidth(160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)

        title = QLabel("Workspace")
        title.setObjectName("paneTitle")
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
        projects_sep = QFrame()
        projects_sep.setFrameShape(QFrame.Shape.HLine)
        projects_sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        layout.addWidget(projects_sep)

        projects_title = QLabel("Projects")
        projects_title.setObjectName("paneTitle")
        layout.addWidget(projects_title)

        self._project_name_label = QLabel("")
        self._project_name_label.setObjectName("projectNameLabel")
        self._project_name_label.setWordWrap(True)
        self._project_name_label.setVisible(False)
        layout.addWidget(self._project_name_label)

        self._thread_list = QListWidget()
        self._thread_list.setVisible(False)
        self._thread_list.setMaximumHeight(200)
        self._thread_list.itemClicked.connect(self._on_thread_clicked)
        layout.addWidget(self._thread_list)

        self._tree = WorkspaceTree(workspace_root)
        layout.addWidget(self._tree, 1)

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
        model_label.setObjectName("paneTitle")
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

    def tree(self) -> WorkspaceTree:
        return self._tree

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

    def refresh_projects(self, workspace_root: Path | None) -> None:
        """Update the project name and thread list for the given workspace root."""
        if workspace_root is None:
            self._project_name_label.setVisible(False)
            self._thread_list.setVisible(False)
            return

        try:
            store = ProjectStore()
            project = store.create_or_update_project(workspace_root)
        except Exception:
            self._project_name_label.setVisible(False)
            self._thread_list.setVisible(False)
            return

        self._project_name_label.setText(project.name)
        self._project_name_label.setVisible(True)

        threads = store.list_threads(project, include_archived=False)
        self._thread_list.clear()
        for t in threads:
            item = QListWidgetItem(t.title)
            item.setData(Qt.UserRole, str(t.conversation_path) if t.conversation_path else "")
            self._thread_list.addItem(item)

        self._thread_list.setVisible(len(threads) > 0)

    def _on_thread_clicked(self, item: QListWidgetItem) -> None:
        """Emit the conversation_path for the clicked thread."""
        path_str = item.data(Qt.UserRole)
        if path_str:
            self.thread_selected.emit(Path(path_str))


def _models_with_default(provider: ProviderId) -> dict[str, ModelInfo]:
    spec = provider_registry.get(provider)
    models = dict(spec.models)
    if spec.default_model not in models:
        models[spec.default_model] = ModelInfo(
            id=spec.default_model,
            label=spec.default_model.split("/")[-1].replace("-", " ").title(),
            input_per_m_usd=0.0,
            output_per_m_usd=0.0,
            cache_hit_per_m_usd=0.0,
        )
    # For DeepSeek, ensure deepseek-v4-pro is always present
    if provider == "deepseek":
        from aura.providers.catalog import DEFAULT_WORKER_MODEL

        if DEFAULT_WORKER_MODEL not in models:
            models[DEFAULT_WORKER_MODEL] = ModelInfo(
                id=DEFAULT_WORKER_MODEL,
                label=DEFAULT_WORKER_MODEL,
                input_per_m_usd=0.0,
                output_per_m_usd=0.0,
                cache_hit_per_m_usd=0.0,
            )
    return models
