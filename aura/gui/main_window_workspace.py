from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from aura.config import save_workspace_root
from aura.drones.construction_context import clear_drone_construction
from aura.git_ops import git_init, is_git_repo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow


class MainWindowWorkspaceController(QObject):
    """Owns the Workspace / Project Navigation responsibility cluster for MainWindow."""

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window

    def on_change_root(self) -> None:
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose workspace root", start)
        if not chosen:
            return
        path = Path(chosen)
        self._on_project_selected(path)

        # Offer to initialize git if the workspace is not a git repo.
        if not is_git_repo(path):
            reply = QMessageBox.question(
                window,
                "Not a Git Repository",
                "This workspace is not a git repository.\n\n"
                "Aura uses git for auto-commit and undo.\n"
                "Would you like to run 'git init' and create an initial commit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                ok, msg = git_init(path)
                if ok:
                    QMessageBox.information(window, "Git Repository", msg)
                else:
                    QMessageBox.warning(window, "Git Init Failed", msg)

    def _retarget_workspace(self, root_path: Path, *, restore_last: bool = True) -> None:
        from aura.drones.store import _project_root_for_drone_storage
        storage_root = _project_root_for_drone_storage(root_path).resolve()
        window = self._window
        clear_drone_construction()
        if window._workspace_root is not None and window._workspace_root.resolve() != storage_root:
            window._persistence.new_conversation()
        window._workspace_root = storage_root
        window._checkpoint_dialog = None
        window._bridge.set_workspace_root(storage_root)
        window._input.set_workspace_root(storage_root)
        window._send_handler.set_workspace_root(storage_root)
        window._playground.set_workspace_root(storage_root)
        window._companion.set_workspace_root(str(window._workspace_root))
        window._tree.set_root(storage_root)
        self.update_workspace_label()
        window._refresh_status_bar()
        # Switch from launchpad to workspace view
        window._switch_to_workspace_view()
        if window._settings.restore_last_conversation and restore_last:
            QTimer.singleShot(0, lambda: window._persistence.restore_last(storage_root))

    def _on_project_selected(self, root_path: Path, *, restore_last: bool = True) -> None:
        from aura.projects.store import ProjectStore
        project = ProjectStore().create_or_update_project(root_path)
        window = self._window
        window._companion.set_current_project(project.id, project.name)
        save_workspace_root(root_path)
        self._retarget_workspace(root_path, restore_last=restore_last)
        window._left_pane.refresh_projects(window._workspace_root)
        window._left_pane.refresh_drones(window._workspace_root)

    def on_new_project(self) -> None:
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose or Create Workspace Directory", start)
        if not chosen:
            return
        chosen_path = Path(chosen)
        from aura.projects.store import ProjectStore
        ProjectStore().create_or_update_project(chosen_path)
        self._on_project_selected(chosen_path)

    def onboarding_change_workspace(self) -> str | None:
        """Called from onboarding dialog to change workspace. Returns new path or None."""
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose workspace root", start)
        if not chosen:
            return None
        path = Path(chosen)
        window._workspace_root = path
        window._bridge.set_workspace_root(path)
        window._input.set_workspace_root(path)
        window._send_handler.set_workspace_root(path)
        window._playground.set_workspace_root(path)
        window._companion.set_workspace_root(str(window._workspace_root))
        window._tree.set_root(path)
        save_workspace_root(path)
        from aura.projects.store import ProjectStore
        _project = ProjectStore().create_or_update_project(path)
        window._companion.set_current_project(_project.id, _project.name)
        self.update_workspace_label()
        window._left_pane.refresh_projects(path)
        window._left_pane.refresh_drones(path)
        # Close drone workbay when workspace root changes
        window._drone_controller.hide_workbay()
        clear_drone_construction()
        window._refresh_status_bar()
        return str(path)

    def on_open_existing(self) -> None:
        """Let user pick an existing folder as workspace root."""
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Open Project", start)
        if not chosen:
            return
        path = Path(chosen)
        self._on_project_selected(path)
        if not is_git_repo(path):
            reply = QMessageBox.question(
                window,
                "Not a Git Repository",
                "This workspace is not a git repository.\n\n"
                "Aura uses git for auto-commit and undo.\n"
                "Would you like to run 'git init' and create an initial commit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                ok, msg = git_init(path)
                if ok:
                    QMessageBox.information(window, "Git Repository", msg)
                else:
                    QMessageBox.warning(window, "Git Init Failed", msg)

    def on_create_new_project(self) -> None:
        """Let user choose or create an empty folder, then set it as workspace."""
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Create Project Folder", start)
        if not chosen:
            return
        path = Path(chosen)
        self._on_project_selected(path)
        if not is_git_repo(path):
            reply = QMessageBox.question(
                window,
                "Not a Git Repository",
                "This workspace is not a git repository.\n\n"
                "Aura uses git for auto-commit and undo.\n"
                "Would you like to run 'git init' and create an initial commit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                ok, msg = git_init(path)
                if ok:
                    QMessageBox.information(window, "Git Repository", msg)
                else:
                    QMessageBox.warning(window, "Git Init Failed", msg)

    def on_create_demo_project(self) -> None:
        """Create a tiny demo project suitable for first-time users."""
        window = self._window
        home = Path.home()
        projects_root = home / "Documents" / "Aura Projects"
        demo_dir = projects_root / "hello-aura"
        demo_dir.mkdir(parents=True, exist_ok=True)

        # Write README.md
        readme_content = (
            "# Hello, Aura\n\n"
            "This is a safe demo project for trying the "
            "Planner \u2192 Worker \u2192 Diff \u2192 Validation loop.\n\n"
            "Use the input panel to ask Aura to add a small feature, "
            "then review the diff and let the Worker validate it.\n"
        )
        (demo_dir / "README.md").write_text(readme_content, encoding="utf-8")

        # Write src/main.py
        src_dir = demo_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        main_content = (
            "def greet(name: str) -> str:\n"
            '    return f"Hello, {name}! Welcome to Aura."\n\n'
            "\n"
            'if __name__ == "__main__":\n'
            '    print(greet("Developer"))\n'
        )
        (src_dir / "main.py").write_text(main_content, encoding="utf-8")

        # Git init if possible (non-fatal if it fails)
        if not is_git_repo(demo_dir):
            try:
                git_init(demo_dir)
            except Exception as exc:
                logger.warning("git init for demo project failed: %s", exc)

        # Select as workspace
        self._on_project_selected(demo_dir)

    def update_workspace_label(self) -> None:
        self._window._left_pane.update_workspace_label(self._window._workspace_root)
