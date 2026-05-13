"""Git checkpoint history dialog."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.git_ops import (
    commit_diff,
    recent_commits,
    restore_to_snapshot,
    working_tree_status,
)
from aura.gui.theme import BG, BORDER, FG, FG_DIM, FG_MUTED


class CommitDiffDialog(QDialog):
    """Read-only dialog showing a git commit patch."""

    def __init__(self, title: str, diff_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(960, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(10)

        self._diff_view = QPlainTextEdit(self)
        self._diff_view.setReadOnly(True)
        self._diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Geist Mono, JetBrains Mono, Consolas, Menlo, monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setFixedPitch(True)
        mono.setPointSize(10)
        self._diff_view.setFont(mono)
        self._diff_view.setPlainText(diff_text)
        self._diff_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 8px; }}"
        )
        layout.addWidget(self._diff_view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)


class CheckpointDialog(QDialog):
    """Shows recent git commits and restore actions for a workspace."""

    def __init__(self, workspace_root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root

        self.setWindowTitle("Checkpoints")
        self.setModal(True)
        self.resize(760, 620)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(12)

        header = QLabel("Recent git checkpoints", self)
        header.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 15px;")
        outer.addWidget(header)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color: {FG_DIM};")
        outer.addWidget(self._summary)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._list_host = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(10)
        self._scroll.setWidget(self._list_host)
        outer.addWidget(self._scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)

        refresh_btn = QPushButton("Refresh", self)
        refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(refresh_btn)

        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.reject)
        actions.addWidget(close_btn)

        outer.addLayout(actions)

        self.refresh()

    def workspace_root(self) -> Path:
        return self._workspace_root

    def refresh(self) -> None:
        """Reload recent commits and rebuild the row list."""
        self._clear_rows()
        self._summary.setText("Loading checkpoints...")

        ok, commits, message = recent_commits(self._workspace_root)
        if not ok:
            self._summary.setText(message or "Could not load checkpoint history.")
            self._add_empty_row("No checkpoint history is available for this workspace.")
            return

        if not commits:
            self._summary.setText("No commits found in this workspace.")
            self._add_empty_row("No checkpoints yet.")
            return

        self._summary.setText(f"Showing {len(commits)} most recent checkpoint(s).")
        for commit in commits:
            self._add_commit_row(commit)
        self._list_layout.addStretch(1)

    def _clear_rows(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_empty_row(self, text: str) -> None:
        label = QLabel(text, self._list_host)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {FG_DIM}; padding: 12px;")
        self._list_layout.addWidget(label)
        self._list_layout.addStretch(1)

    def _add_commit_row(self, commit: dict) -> None:
        card = QFrame(self._list_host)
        card.setObjectName("card")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)

        subject = QLabel(str(commit.get("subject") or "(no subject)"), card)
        subject.setWordWrap(True)
        subject.setStyleSheet(f"color: {FG}; font-weight: 600;")
        top.addWidget(subject, 1)

        diff_btn = QPushButton("View Diff", card)
        diff_btn.clicked.connect(lambda _checked=False, c=commit: self._on_view_diff(c))
        top.addWidget(diff_btn)

        restore_btn = QPushButton("Restore", card)
        restore_btn.setObjectName("danger")
        restore_btn.clicked.connect(lambda _checked=False, c=commit: self._on_restore(c))
        top.addWidget(restore_btn)

        layout.addLayout(top)

        count = commit.get("changed_files_count")
        count_text = "unknown files" if count is None else f"{count} changed file(s)"
        meta = QLabel(
            f"{commit.get('short_sha', '')} - {commit.get('relative_date', '')} - "
            f"{commit.get('author', '')} - {count_text}",
            card,
        )
        meta.setWordWrap(True)
        meta.setStyleSheet(f"color: {FG_DIM};")
        layout.addWidget(meta)

        files = list(commit.get("changed_files") or [])
        if files:
            shown = files[:5]
            suffix = "" if len(files) <= 5 else f" +{len(files) - 5} more"
            files_label = QLabel(f"Files: {', '.join(shown)}{suffix}", card)
            files_label.setWordWrap(True)
            files_label.setStyleSheet(f"color: {FG_MUTED};")
            layout.addWidget(files_label)

        self._list_layout.addWidget(card)

    def _on_view_diff(self, commit: dict) -> None:
        sha = str(commit.get("sha") or "")
        if not sha:
            QMessageBox.warning(self, "Diff Unavailable", "This checkpoint has no commit SHA.")
            return

        ok, diff_text, message = commit_diff(self._workspace_root, sha)
        if not ok:
            QMessageBox.warning(
                self,
                "Diff Unavailable",
                message or "Could not load the checkpoint diff.",
            )
            return

        title = f"Checkpoint Diff - {commit.get('short_sha', sha[:8])}"
        CommitDiffDialog(title, diff_text, self).exec()

    def _on_restore(self, commit: dict) -> None:
        sha = str(commit.get("sha") or "")
        short_sha = str(commit.get("short_sha") or sha[:8])
        if not sha:
            QMessageBox.warning(self, "Restore Unavailable", "This checkpoint has no commit SHA.")
            return

        ok, status, message = working_tree_status(self._workspace_root)
        if not ok:
            QMessageBox.warning(
                self,
                "Restore Unavailable",
                message or "Could not inspect the working tree.",
            )
            return

        dirty = bool(status.strip())
        warning = (
            f"Restore this workspace to checkpoint {short_sha}?\n\n"
            "Aura will run git reset --hard. This replaces the current workspace "
            "with the selected checkpoint."
        )
        if dirty:
            warning += "\n\nUncommitted changes are present and will be discarded."

        reply = QMessageBox.warning(
            self,
            "Restore Checkpoint",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        restored, restore_message = restore_to_snapshot(self._workspace_root, sha)
        if restored:
            QMessageBox.information(
                self,
                "Checkpoint Restored",
                restore_message or f"Restored to {short_sha}.",
            )
            self.refresh()
        else:
            QMessageBox.warning(
                self,
                "Restore Failed",
                restore_message or "Could not restore the checkpoint.",
            )

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
