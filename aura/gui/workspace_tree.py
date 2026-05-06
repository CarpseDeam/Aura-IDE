"""Read-only workspace tree pane.

Backed by QFileSystemModel + a thin proxy that hides clutter (dotfiles except
`.aura`, build/cache directories). Double-click opens
files in the OS default editor; right-click shows reveal/copy actions.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    QDir,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QFileSystemModel,
    QHeaderView,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


# Mirrors the SKIP rules in conversation/tools/fs_read.py so the user sees
# what the tools see — minus `.aura`, which we keep visible so backups are
# discoverable.
_HIDDEN_DIRS = {"__pycache__", ".venv", ".git", "node_modules"}
_HIDDEN_SUFFIXES = set()


class _WorkspaceFilterProxy(QSortFilterProxyModel):
    """Hides dotfiles (except `.aura/`) and the cache/build directories."""

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # type: ignore[override]
        model = self.sourceModel()
        if model is None:
            return True
        idx = model.index(source_row, 0, source_parent)
        if not idx.isValid():
            return True
        name = model.fileName(idx)
        if not name or name in (".", ".."):
            return False
        is_dir = model.isDir(idx)
        if is_dir:
            if name in _HIDDEN_DIRS:
                return False
            # Hide dotfile dirs except `.aura` (so user sees backups exist).
            if name.startswith(".") and name != ".aura":
                return False
            return True
        # Files
        if name.startswith("."):
            return False
        suffix = os.path.splitext(name)[1].lower()
        if suffix in _HIDDEN_SUFFIXES:
            return False
        return True


class WorkspaceTree(QWidget):
    """Filtered file tree rooted at the current workspace.

    Read-only — clicking a file opens it in the OS default editor.
    """

    file_activated = Signal(Path)

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._fs_model = QFileSystemModel(self)
        self._fs_model.setFilter(
            QDir.Filter.AllDirs
            | QDir.Filter.Files
            | QDir.Filter.NoDotAndDotDot
            | QDir.Filter.Hidden  # let proxy decide; otherwise `.aura` would be hidden
        )
        # setNameFilters works on file *display*, not directories. Combined with
        # the proxy above, this gives us coverage on both axes.
        self._fs_model.setNameFilterDisables(False)

        self._proxy = _WorkspaceFilterProxy(self)
        self._proxy.setSourceModel(self._fs_model)

        self._view = QTreeView(self)
        self._view.setModel(self._proxy)
        self._view.setHeaderHidden(False)
        self._view.setUniformRowHeights(True)
        self._view.setAnimated(False)
        self._view.setSortingEnabled(False)
        self._view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._view.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.doubleClicked.connect(self._on_double_clicked)

        self._view.setStyleSheet(
            f"QTreeView {{ border: none; background: transparent; }} "
            f"QTreeView::item {{ padding: 4px 6px; }}"
        )

        # Show only the file name column (size/type/date are noise here).
        for col in range(1, 4):
            self._view.setColumnHidden(col, True)
        header = self._view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)

        layout.addWidget(self._view)

        self._root: Path | None = None
        if root is not None:
            self.set_root(root)

    # ---- public API -------------------------------------------------------

    def set_root(self, root: Path) -> None:
        self._root = root
        src_index = self._fs_model.setRootPath(str(root))
        proxy_root = self._proxy.mapFromSource(src_index)
        self._view.setRootIndex(proxy_root)

    def root(self) -> Path | None:
        return self._root

    # ---- handlers ---------------------------------------------------------

    def _on_double_clicked(self, proxy_index: QModelIndex) -> None:
        path = self._path_for(proxy_index)
        if path is None or path.is_dir():
            return
        self.file_activated.emit(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_context_menu(self, pos) -> None:
        proxy_index = self._view.indexAt(pos)
        if not proxy_index.isValid():
            return
        path = self._path_for(proxy_index)
        if path is None:
            return

        menu = QMenu(self)
        reveal_act = QAction("Reveal in Explorer", menu)
        reveal_act.triggered.connect(lambda: self._reveal(path))
        menu.addAction(reveal_act)

        copy_abs = QAction("Copy path", menu)
        copy_abs.triggered.connect(lambda: self._copy_to_clipboard(str(path)))
        menu.addAction(copy_abs)

        copy_rel = QAction("Copy relative path", menu)
        copy_rel.triggered.connect(lambda: self._copy_to_clipboard(self._rel_path(path)))
        menu.addAction(copy_rel)

        menu.exec(self._view.viewport().mapToGlobal(pos))

    # ---- helpers ----------------------------------------------------------

    def _path_for(self, proxy_index: QModelIndex) -> Path | None:
        if not proxy_index.isValid():
            return None
        src_index = self._proxy.mapToSource(proxy_index)
        raw = self._fs_model.filePath(src_index)
        return Path(raw) if raw else None

    def _rel_path(self, path: Path) -> str:
        if self._root is None:
            return str(path)
        try:
            return path.resolve().relative_to(self._root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _reveal(self, path: Path) -> None:
        if sys.platform == "win32":
            # `explorer /select,<path>` highlights the file in its parent dir.
            try:
                subprocess.run(["explorer", f"/select,{path}"], check=False)
            except OSError:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _copy_to_clipboard(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
