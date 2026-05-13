"""Source-install Git update dialog for Aura."""
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

from aura.updater import PullResult, UpdateStatus, get_update_status, pull_latest


class UpdateWorker(QObject):
    output = Signal(str)
    finished = Signal(object)

    def __init__(self, action: str, repo_root: Path | None = None) -> None:
        super().__init__()
        self._action = action
        self._repo_root = repo_root

    def run(self) -> None:
        if self._action == "pull":
            result = pull_latest(self._repo_root, output_callback=self.output.emit)
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

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self._repo_label = QLabel("(not checked)")
        self._repo_label.setWordWrap(True)
        self._branch_label = QLabel("(not checked)")
        self._commit_label = QLabel("(not checked)")
        self._upstream_label = QLabel("(not checked)")
        self._state_label = QLabel("(not checked)")

        form.addRow("Aura repo:", self._repo_label)
        form.addRow("Current branch:", self._branch_label)
        form.addRow("Current commit:", self._commit_label)
        form.addRow("Upstream branch:", self._upstream_label)
        form.addRow("Status:", self._state_label)
        outer.addLayout(form)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setPlaceholderText("Git output and errors will appear here.")
        outer.addWidget(self._output, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)

        self._check_btn = QPushButton("Check for Updates")
        self._check_btn.clicked.connect(self._on_check)
        actions.addWidget(self._check_btn)

        self._pull_btn = QPushButton("Pull Latest")
        self._pull_btn.setObjectName("primary")
        self._pull_btn.setEnabled(False)
        self._pull_btn.clicked.connect(self._on_pull)
        actions.addWidget(self._pull_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)

        outer.addLayout(actions)

    def reject(self) -> None:  # type: ignore[override]
        if self._thread is not None:
            self._append_output("Wait for the current git command to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread is not None:
            self._append_output("Wait for the current git command to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def _on_check(self) -> None:
        self._output.clear()
        self._append_output("Checking Aura source checkout...")
        self._start_worker("check")

    def _on_pull(self) -> None:
        self._append_output("Running git pull --ff-only...")
        self._start_worker("pull")

    def _start_worker(self, action: str) -> None:
        if self._thread is not None:
            return

        self._set_busy(True)
        self._thread = QThread(self)
        self._worker = UpdateWorker(action, self._repo_root)
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
        self._thread = None
        self._worker = None

    def _set_busy(self, busy: bool) -> None:
        self._check_btn.setEnabled(not busy)
        can_pull = self._last_status.can_pull if self._last_status else False
        self._pull_btn.setEnabled((not busy) and can_pull)
        if busy:
            self._pull_btn.setEnabled(False)

    def _on_worker_finished(self, result: object) -> None:
        if isinstance(result, UpdateStatus):
            self._show_status(result)
        elif isinstance(result, PullResult):
            self._show_pull_result(result)
        self._set_busy(False)

    def _show_status(self, status: UpdateStatus) -> None:
        self._last_status = status
        self._repo_root = status.repo_root

        self._repo_label.setText(str(status.repo_root) if status.repo_root else "(not a source checkout)")
        self._branch_label.setText(status.branch or "(unknown)")
        self._commit_label.setText(status.commit or "(unknown)")
        self._upstream_label.setText(status.upstream or "(none)")

        state = self._state_text(status)
        self._state_label.setText(state)
        self._summary.setText(status.message)
        if status.message:
            self._append_output(status.message)
        if status.error:
            self._append_output(status.error)

        self._pull_btn.setEnabled(status.can_pull)

    def _show_pull_result(self, result: PullResult) -> None:
        self._append_output(result.message)
        if result.error:
            self._append_output(result.error)

        if result.success:
            old_commit = _short(result.old_commit)
            new_commit = _short(result.new_commit)
            self._summary.setText(
                "Update succeeded. Restart Aura to use the updated code."
            )
            self._state_label.setText("Update succeeded")
            self._commit_label.setText(new_commit or "(unknown)")
            self._append_output(f"Old commit: {old_commit or '(unknown)'}")
            self._append_output(f"New commit: {new_commit or '(unknown)'}")
            self._append_output("Restart Aura to use the updated code.")
            self._last_status = None
            self._pull_btn.setEnabled(False)
        else:
            self._summary.setText(result.message or "Update failed.")

    def _state_text(self, status: UpdateStatus) -> str:
        if status.state == "up_to_date":
            return "Up to date"
        if status.state == "behind":
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
