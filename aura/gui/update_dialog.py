"""Updater dialog for Aura, supporting both Git and packaged updates."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.updater import (
    GitHubRelease,
    PullResult,
    UpdateStatus,
    get_update_status,
    install_packaged_update,
    is_packaged,
    pull_latest,
)


class UpdateWorker(QObject):
    output = Signal(str)
    finished = Signal(object)

    def __init__(
        self,
        action: str,
        repo_root: Path | None = None,
        release: GitHubRelease | None = None,
    ) -> None:
        super().__init__()
        self._action = action
        self._repo_root = repo_root
        self._release = release

    def run(self) -> None:
        if self._action == "pull":
            result = pull_latest(self._repo_root, output_callback=self.output.emit)
        elif self._action == "install":
            if self._release:
                result = install_packaged_update(self._release, output_callback=self.output.emit)
            else:
                result = PullResult(False, None, message="No release selected.")
        else:
            result = get_update_status(self._repo_root, output_callback=self.output.emit)
        self.finished.emit(result)


class UpdateDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update Aura")
        self.setModal(True)
        self.resize(680, 460)

        self._repo_root: Path | None = None
        self._thread: QThread | None = None
        self._worker: UpdateWorker | None = None
        self._last_status: UpdateStatus | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(12)

        self._summary = QLabel("Click Check for Updates to inspect this Aura install.")
        self._summary.setWordWrap(True)
        outer.addWidget(self._summary)

        self._form = QFormLayout()
        self._form.setHorizontalSpacing(14)
        self._form.setVerticalSpacing(8)

        # Dynamic labels depending on mode
        self._row1_label = QLabel("Aura repo:")
        self._row2_label = QLabel("Current branch:")
        self._row3_label = QLabel("Current commit:")
        self._row4_label = QLabel("Upstream branch:")

        self._row1_val = QLabel("(not checked)")
        self._row1_val.setWordWrap(True)
        self._row2_val = QLabel("(not checked)")
        self._row3_val = QLabel("(not checked)")
        self._row4_val = QLabel("(not checked)")
        self._state_label = QLabel("(not checked)")

        self._form.addRow(self._row1_label, self._row1_val)
        self._form.addRow(self._row2_label, self._row2_val)
        self._form.addRow(self._row3_label, self._row3_val)
        self._form.addRow(self._row4_label, self._row4_val)
        self._form.addRow("Status:", self._state_label)
        outer.addLayout(self._form)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setPlaceholderText("Output and errors will appear here.")
        outer.addWidget(self._output, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)

        self._check_btn = QPushButton("Check for Updates")
        self._check_btn.clicked.connect(self._on_check)
        actions.addWidget(self._check_btn)

        self._action_btn = QPushButton("Update Now")
        self._action_btn.setObjectName("primary")
        self._action_btn.setEnabled(False)
        self._action_btn.clicked.connect(self._on_action)
        actions.addWidget(self._action_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)

        outer.addLayout(actions)

        # Initial mode setup
        if is_packaged():
            self._setup_packaged_ui()
        else:
            self._setup_git_ui()

    def _setup_git_ui(self) -> None:
        self._row1_label.setText("Aura repo:")
        self._row2_label.setText("Current branch:")
        self._row3_label.setText("Current commit:")
        self._row4_label.setText("Upstream branch:")
        self._action_btn.setText("Pull Latest")

    def _setup_packaged_ui(self) -> None:
        self._row1_label.setText("Install mode:")
        self._row1_val.setText("Packaged (Windows)")
        self._row2_label.setText("Current version:")
        self._row3_label.setText("Latest version:")
        self._row4_label.setText("Update asset:")
        self._action_btn.setText("Install Update")

    def reject(self) -> None:  # type: ignore[override]
        if self._thread is not None:
            self._append_output("Wait for the current operation to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread is not None:
            self._append_output("Wait for the current operation to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def _on_check(self) -> None:
        self._output.clear()
        if is_packaged():
            self._append_output("Checking for latest GitHub release...")
        else:
            self._append_output("Checking Aura source checkout...")
        self._start_worker("check")

    def _on_action(self) -> None:
        if is_packaged():
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Install Update",
                "Aura will close, install the update, and relaunch. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._append_output("Downloading and installing update...")
            self._start_worker("install")
        else:
            self._append_output("Running git pull --ff-only...")
            self._start_worker("pull")

    def _start_worker(self, action: str) -> None:
        if self._thread is not None:
            return

        self._set_busy(True)
        self._thread = QThread(self)
        release = self._last_status.release if self._last_status else None
        self._worker = UpdateWorker(action, self._repo_root, release)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.output.connect(self._append_output)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._clear_worker)
        self._thread.start()

    def _clear_worker(self) -> None:
        pass

    def _set_busy(self, busy: bool) -> None:
        self._check_btn.setEnabled(not busy)
        can_act = False
        if self._last_status:
            if is_packaged():
                can_act = self._last_status.can_install
            else:
                can_act = self._last_status.can_pull
        
        self._action_btn.setEnabled((not busy) and can_act)
        if busy:
            self._action_btn.setEnabled(False)

    def _on_worker_finished(self, result: object) -> None:
        if isinstance(result, UpdateStatus):
            self._show_status(result)
        elif isinstance(result, PullResult):
            self._show_pull_result(result)
        self._set_busy(False)

    def _show_status(self, status: UpdateStatus) -> None:
        self._last_status = status
        self._repo_root = status.repo_root

        if status.is_packaged:
            self._row2_val.setText(status.current_version)
            self._row3_val.setText(status.latest_version or "(unknown)")
            asset = status.release.packaged_asset if status.release else None
            self._row4_val.setText(asset.name if asset else "(none)")
            self._action_btn.setEnabled(status.can_install)
        else:
            self._row1_val.setText(str(status.repo_root) if status.repo_root else "(not a source checkout)")
            self._row2_val.setText(status.branch or "(unknown)")
            self._row3_val.setText(status.commit or "(unknown)")
            self._row4_val.setText(status.upstream or "(none)")
            self._action_btn.setEnabled(status.can_pull)

        state = self._state_text(status)
        self._state_label.setText(state)
        self._summary.setText(status.message)
        if status.message:
            self._append_output(status.message)
        if status.error:
            self._append_output(status.error)

    def _show_pull_result(self, result: PullResult) -> None:
        self._append_output(result.message)
        if result.error:
            self._append_output(result.error)

        if result.success:
            if is_packaged():
                self._summary.setText("Update script launched. Aura will now exit.")
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication
                # Automatically exit after a short delay to allow the script to take over
                QTimer.singleShot(2000, QApplication.quit)
            else:
                old_commit = _short(result.old_commit)
                new_commit = _short(result.new_commit)
                self._summary.setText(
                    "Update succeeded. Restart Aura to use the updated code."
                )
                self._state_label.setText("Update succeeded")
                self._row3_val.setText(new_commit or "(unknown)")
                self._append_output(f"Old commit: {old_commit or '(unknown)'}")
                self._append_output(f"New commit: {new_commit or '(unknown)'}")
                self._append_output("Restart Aura to use the updated code.")
                self._last_status = None
                self._action_btn.setEnabled(False)
        else:
            self._summary.setText(result.message or "Update failed.")

    def _state_text(self, status: UpdateStatus) -> str:
        if status.state == "up_to_date":
            return "Up to date"
        if status.state == "behind":
            if status.is_packaged:
                return "Update available"
            detail = f"Behind by {status.behind} commit(s)"
            if status.has_local_changes:
                detail += " - local changes must be handled first"
            return detail
        if status.state == "ahead":
            return f"Ahead by {status.ahead} commit(s)"
        if status.state == "diverged":
            return (
                f"Diverged: ahead {status.ahead}, behind {status.behind}"
            )
        if status.state == "no_upstream":
            return "No upstream configured"
        if status.state == "not_git":
            return "Not a source git checkout"
        return "Error"

    def _append_output(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._output.toPlainText():
            self._output.appendPlainText("")
        self._output.appendPlainText(text)


def _short(commit: str | None) -> str | None:
    if not commit:
        return None
    return commit[:8]
