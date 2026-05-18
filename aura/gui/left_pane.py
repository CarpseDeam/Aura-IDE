from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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

class LeftPane(QFrame):
    change_root_requested = Signal()
    planner_model_changed = Signal(str)
    planner_thinking_changed = Signal(str)
    worker_model_changed = Signal(str)
    worker_thinking_changed = Signal(str)
    planner_backend_changed = Signal(str)  # 'default_api' or 'gemini_cli'
    worker_backend_changed = Signal(str)   # 'default_api' or 'gemini_cli'

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

        self._tree = WorkspaceTree(workspace_root)
        layout.addWidget(self._tree, 1)

        # --- Model Config section ---
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        layout.addWidget(sep)

        model_label = QLabel("Model Configuration")
        model_label.setObjectName("paneTitle")
        layout.addWidget(model_label)

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
        layout.addLayout(planner_model_row)

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
        layout.addLayout(planner_think_row)

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
        layout.addLayout(worker_model_row)

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
        layout.addLayout(worker_think_row)

        # --- Backend Selection section ---
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        layout.addWidget(sep2)

        backend_label = QLabel("Agent Backends")
        backend_label.setObjectName("paneTitle")
        layout.addWidget(backend_label)

        # Planner backend
        planner_backend_row = QHBoxLayout()
        planner_backend_row.setSpacing(4)
        planner_backend_lbl = QLabel("Planner:")
        planner_backend_lbl.setStyleSheet(f"color: {FG_DIM};")
        planner_backend_row.addWidget(planner_backend_lbl)
        self._planner_backend_combo = QComboBox()
        self._planner_backend_combo.addItem("Default API", "default_api")
        self._planner_backend_combo.addItem("Gemini CLI", "gemini_cli")
        self._planner_backend_combo.addItem("Claude Code", "claude_code")
        self._planner_backend_combo.addItem("Codex", "codex")
        self._planner_backend_combo.currentIndexChanged.connect(
            lambda: self.planner_backend_changed.emit(self._planner_backend_combo.currentData())
        )
        planner_backend_row.addWidget(self._planner_backend_combo, 1)
        layout.addLayout(planner_backend_row)

        # Worker backend
        worker_backend_row = QHBoxLayout()
        worker_backend_row.setSpacing(4)
        worker_backend_lbl = QLabel("Worker:")
        worker_backend_lbl.setStyleSheet(f"color: {FG_DIM};")
        worker_backend_row.addWidget(worker_backend_lbl)
        self._worker_backend_combo = QComboBox()
        self._worker_backend_combo.addItem("Default API", "default_api")
        self._worker_backend_combo.addItem("Gemini CLI", "gemini_cli")
        self._worker_backend_combo.addItem("Claude Code", "claude_code")
        self._worker_backend_combo.addItem("Codex", "codex")
        self._worker_backend_combo.currentIndexChanged.connect(
            lambda: self.worker_backend_changed.emit(self._worker_backend_combo.currentData())
        )
        worker_backend_row.addWidget(self._worker_backend_combo, 1)
        layout.addLayout(worker_backend_row)

        self.update_workspace_label(workspace_root)

    def tree(self) -> WorkspaceTree:
        return self._tree

    def update_workspace_label(self, root: Path | None) -> None:
        if root is None:
            self._workspace_label.setText("(none)")
            return
        self._workspace_label.setText(str(root))

    def populate_models(self, planner_provider: ProviderId, worker_provider: ProviderId) -> None:
        p_spec = provider_registry.get(planner_provider)
        w_spec = provider_registry.get(worker_provider)

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

    def current_planner_backend(self) -> str:
        return self._planner_backend_combo.currentData()

    def current_worker_backend(self) -> str:
        return self._worker_backend_combo.currentData()

    def set_planner_backend(self, backend: str) -> None:
        idx = self._planner_backend_combo.findData(backend)
        if idx >= 0:
            self._planner_backend_combo.setCurrentIndex(idx)

    def set_worker_backend(self, backend: str) -> None:
        idx = self._worker_backend_combo.findData(backend)
        if idx >= 0:
            self._worker_backend_combo.setCurrentIndex(idx)

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._worker_model_label.setVisible(enabled)
        self._worker_model_combo.setVisible(enabled)
        self._worker_thinking_label.setVisible(enabled)
        self._worker_thinking_combo.setVisible(enabled)


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
    return models
