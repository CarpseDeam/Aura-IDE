from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal, QThread
from PySide6.QtWidgets import QFileDialog, QMessageBox

from aura.config import save_settings, save_workspace_root
from aura.drones.construction_context import clear_drone_construction
from aura.git_ops import git_init, is_git_repo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow


def _categorize_blocked_root(path: Path) -> str | None:
    """Return a category string if path is a blocked broad root, else None."""
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return None
    home = Path.home().resolve()
    if resolved.parent == resolved:
        return "filesystem root"
    if resolved == home:
        return "home folder"
    if resolved == home / "Desktop":
        return "Desktop"
    if resolved == home / "Downloads":
        return "Downloads"
    if resolved == home / "Documents":
        return "Documents"
    if resolved == home / "OneDrive":
        return "OneDrive"
    return None


class _GitCheckWorker(QObject):
    """Runs is_git_repo on a thread so the 5s timeout never blocks the UI."""
    finished = Signal(Path, bool)  # path, is_git

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        result = is_git_repo(self._path)
        self.finished.emit(self._path, result)


class MainWindowWorkspaceController(QObject):
    """Owns the Workspace / Project Navigation responsibility cluster for MainWindow."""

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._pending_git_path: Path | None = None
        self._active_git_worker: _GitCheckWorker | None = None
        self._active_git_thread: QThread | None = None

    def _warn_blocked_root(self, path: Path) -> bool:
        """Return True if path was blocked (warning shown), False if OK to proceed."""
        category = _categorize_blocked_root(path)
        if category is None:
            return False
        logger.warning("Blocked workspace root selection: %s", category)
        QMessageBox.warning(
            self._window,
            "Workspace Root Too Broad",
            f"Aura needs a specific project folder, not a broad system folder like {category}.\n\n"
            "Please choose a project-specific folder as your workspace root.",
        )
        return True

    def _show_non_git_warning(self, root_path: Path) -> None:
        """Non-modal warning that the folder is not a Git repo."""
        msg_box = QMessageBox(self._window)
        msg_box.setWindowTitle("Not a Git Repository")
        msg_box.setText(
            "This folder is not a Git repo. Aura can still browse it, "
            "but undo and auto-commit need Git."
        )
        init_btn = msg_box.addButton("Initialize Git", QMessageBox.ButtonRole.ActionRole)
        msg_box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        msg_box.setIcon(QMessageBox.Icon.Information)
        msg_box.setModal(False)
        msg_box.setWindowModality(Qt.WindowModality.NonModal)

        def _on_button_clicked(btn):
            if btn == init_btn:
                ok, msg = git_init(root_path)
                if ok:
                    QMessageBox.information(self._window, "Git Repository", msg)
                else:
                    QMessageBox.warning(self._window, "Git Init Failed", msg)
            msg_box.close()

        msg_box.buttonClicked.connect(_on_button_clicked)
        msg_box.show()

    def _check_git_async(self, root_path: Path) -> None:
        """Check git in background; show non-modal warning if not a repo."""
        self._pending_git_path = root_path
        worker = _GitCheckWorker(root_path)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_git_check_done)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._active_git_thread = thread
        self._active_git_worker = worker

    def _on_git_check_done(self, root_path: Path, is_git: bool) -> None:
        """Called from worker thread when git check completes."""
        # Ignore stale results when the user has already changed workspace
        if root_path != self._pending_git_path:
            return
        self._active_git_thread = None
        self._active_git_worker = None
        if not is_git:
            self._show_non_git_warning(root_path)

    def on_change_root(self) -> None:
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose project folder", start)
        if not chosen:
            return
        path = Path(chosen)
        if self._warn_blocked_root(path):
            return
        self._on_project_selected(path)
        self._check_git_async(path)

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
        t0 = time.perf_counter()
        window._tree.set_root(storage_root)
        logger.info("tree.set_root done in %.3fs", time.perf_counter() - t0)
        self.update_workspace_label()
        window._refresh_status_bar()
        # Switch from launchpad to workspace view
        window._switch_to_workspace_view()
        if window._settings.restore_last_conversation and restore_last:
            t1 = time.perf_counter()

            def _do_restore():
                window._persistence.restore_last(storage_root)
                logger.info("restore_last done in %.3fs", time.perf_counter() - t1)

            QTimer.singleShot(0, _do_restore)

        # Mark first launch done once user picks a project
        if not window._settings.first_launch_done:
            window._settings.first_launch_done = True
            save_settings(window._settings)

    def _on_project_selected(self, root_path: Path, *, restore_last: bool = True) -> None:
        from aura.projects.store import ProjectStore
        t0 = time.perf_counter()
        logger.info("create_or_update_project start")
        project = ProjectStore().create_or_update_project(root_path)
        logger.info("create_or_update_project done in %.3fs", time.perf_counter() - t0)
        window = self._window
        window._companion.set_current_project(project.id, project.name)
        save_workspace_root(root_path)
        t_retarget = time.perf_counter()
        self._retarget_workspace(root_path, restore_last=restore_last)
        logger.info("retarget_workspace done in %.3fs", time.perf_counter() - t_retarget)

        def _do_refresh_after_load():
            t1 = time.perf_counter()
            window._left_pane.refresh_projects(window._workspace_root, schedule_backfill=True)
            logger.info("refresh_projects done in %.3fs", time.perf_counter() - t1)
            t2 = time.perf_counter()
            window._left_pane.refresh_drones(window._workspace_root)
            logger.info("refresh_drones done in %.3fs", time.perf_counter() - t2)

        QTimer.singleShot(0, _do_refresh_after_load)

    def on_new_project(self) -> None:
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose or Create Project Folder", start)
        if not chosen:
            return
        chosen_path = Path(chosen)
        if self._warn_blocked_root(chosen_path):
            return
        self._on_project_selected(chosen_path)

    def onboarding_change_workspace(self) -> str | None:
        """Called from onboarding dialog to change workspace. Returns new path or None."""
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Choose project folder", start)
        if not chosen:
            return None
        path = Path(chosen)
        if self._warn_blocked_root(path):
            return None
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

        def _do_refresh_after_load():
            t0 = time.perf_counter()
            window._left_pane.refresh_projects(path, schedule_backfill=True)
            logger.info("refresh_projects done in %.3fs", time.perf_counter() - t0)
            t1 = time.perf_counter()
            window._left_pane.refresh_drones(path)
            logger.info("refresh_drones done in %.3fs", time.perf_counter() - t1)

        QTimer.singleShot(0, _do_refresh_after_load)

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
        if self._warn_blocked_root(path):
            return
        self._on_project_selected(path)
        self._check_git_async(path)

    def on_create_new_project(self) -> None:
        """Let user choose or create an empty folder, then set it as workspace."""
        window = self._window
        start = str(window._workspace_root) if window._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(window, "Create Project Folder", start)
        if not chosen:
            return
        path = Path(chosen)
        if self._warn_blocked_root(path):
            return
        self._on_project_selected(path)
        self._check_git_async(path)

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
        t0 = time.perf_counter()
        not_git = not is_git_repo(demo_dir)
        logger.info("is_git_repo done in %.3fs", time.perf_counter() - t0)
        if not_git:
            try:
                git_init(demo_dir)
            except Exception as exc:
                logger.warning("git init for demo project failed: %s", exc)

        # Select as workspace
        self._on_project_selected(demo_dir)

    def update_workspace_label(self) -> None:
        self._window._left_pane.update_workspace_label(self._window._workspace_root)
