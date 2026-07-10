from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.godot_toolchain import (
    find_godot_project_root,
    load_godot_executable_setting,
    resolve_godot_executable,
    save_godot_executable_setting,
)
from aura.gui.theme import FG_DIM


class SandboxPage(QWidget):
    def __init__(
        self,
        settings: AppSettings,
        workspace_root: Path | None,
        on_change_root: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._on_change_root = on_change_root
        self._godot_project_root = (
            find_godot_project_root(workspace_root) if workspace_root is not None else None
        )
        self._godot_executable_edit: QLineEdit | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # Execution Sandbox
        sandbox_sep = QLabel("Execution Sandbox")
        sandbox_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", sandbox_sep)

        sandbox_note = QLabel(
            "Docker mode runs terminal commands and dynamic tools in an isolated container. "
            "Host mode runs them directly on your machine (fast but no isolation). "
            "Docker must be installed for Docker mode to work."
        )
        sandbox_note.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        sandbox_note.setWordWrap(True)
        form.addRow("", sandbox_note)

        self._sandbox_combo = QComboBox()
        self._sandbox_combo.addItem("Docker (recommended)", "docker")
        self._sandbox_combo.addItem("Host (no isolation)", "host")
        self._sandbox_combo.addItem("WASM (coming soon)", "wasm")
        form.addRow("Sandbox mode:", self._sandbox_combo)

        sandbox_idx = self._sandbox_combo.findData(self._settings.sandbox_mode)
        if sandbox_idx >= 0:
            self._sandbox_combo.setCurrentIndex(sandbox_idx)

        # Workspace
        ws_sep = QLabel("Workspace")
        ws_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", ws_sep)

        ws_row = QHBoxLayout()
        ws_row.setSpacing(8)
        self._ws_label = QLabel(str(workspace_root) if workspace_root else "(none)")
        self._ws_label.setStyleSheet(f"color: {FG_DIM};")
        self._ws_label.setWordWrap(True)
        ws_row.addWidget(self._ws_label, 1)
        change_btn = QPushButton("Change...")
        change_btn.clicked.connect(self._on_change_root_clicked)
        ws_row.addWidget(change_btn)
        ws_widget = QWidget()
        ws_widget.setLayout(ws_row)
        form.addRow("Workspace root:", ws_widget)

        if self._godot_project_root is not None:
            godot_row = QHBoxLayout()
            godot_row.setSpacing(8)
            self._godot_executable_edit = QLineEdit()
            self._godot_executable_edit.setText(self._godot_executable_display_value())
            godot_row.addWidget(self._godot_executable_edit, 1)
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(self._browse_godot_executable)
            godot_row.addWidget(browse_btn)
            godot_widget = QWidget()
            godot_widget.setLayout(godot_row)
            form.addRow("Godot executable:", godot_widget)

        # Backups info
        if workspace_root is not None:
            backup_path = workspace_root / ".aura" / "backups"
            backup_text = (
                f"Stored in {backup_path}, never auto-deleted. Manage manually."
            )
        else:
            backup_text = "Stored under <workspace>/.aura/backups/, never auto-deleted."
        backup_label = QLabel(backup_text)
        backup_label.setStyleSheet(f"color: {FG_DIM};")
        backup_label.setWordWrap(True)
        form.addRow("Backups:", backup_label)

        layout.addLayout(form)
        layout.addStretch()

    def _on_change_root_clicked(self) -> None:
        self._on_change_root()
        # The callback (MainWindow._on_change_root) updates MainWindow._workspace_root.
        # Read it back from the parent chain: SandboxPage → SettingsDialog → MainWindow.
        main_window = self.window().parent() if self.window() else None
        if main_window is not None and hasattr(main_window, "_workspace_root"):
            new_root = getattr(main_window, "_workspace_root")
            self._ws_label.setText(str(new_root) if new_root else "(none)")
            self._godot_project_root = (
                find_godot_project_root(new_root) if new_root is not None else None
            )
            if self._godot_executable_edit is not None:
                self._godot_executable_edit.setEnabled(self._godot_project_root is not None)
                self._godot_executable_edit.setText(self._godot_executable_display_value())

    def collect_settings(self, settings: AppSettings) -> None:
        settings.sandbox_mode = self._sandbox_combo.currentData()

    def apply_project_settings(self) -> None:
        if self._godot_project_root is None or self._godot_executable_edit is None:
            return
        save_godot_executable_setting(
            self._godot_project_root,
            self._godot_executable_edit.text(),
        )

    def _browse_godot_executable(self) -> None:
        if self._godot_executable_edit is None:
            return
        current = self._godot_executable_edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.home() / "Desktop")
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Godot executable",
            start,
            "Godot executable (*.exe);;All files (*)",
        )
        if chosen:
            self._godot_executable_edit.setText(chosen)

    def _godot_executable_display_value(self) -> str:
        if self._godot_project_root is None:
            return ""
        configured = load_godot_executable_setting(self._godot_project_root)
        if configured:
            return configured
        resolution = resolve_godot_executable(self._godot_project_root)
        return str(resolution.path) if resolution.path is not None else ""
