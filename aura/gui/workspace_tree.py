"""Read-only workspace tree pane.

Backed by QFileSystemModel + a thin proxy that hides clutter (dotfiles except
`.aura`, build/cache directories). Double-click opens files inside Aura;
right-click shows Aura/open/reveal/copy actions.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from aura.config import get_subprocess_kwargs
from PySide6.QtCore import (
    QDir,
    QFileInfo,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QFileIconProvider,
    QFileSystemModel,
    QHeaderView,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from aura.config import media_path


# Mirrors the SKIP rules in conversation/tools/fs_read.py so the user sees
# what the tools see — minus `.aura`, which we keep visible so backups are
# discoverable.
_HIDDEN_DIRS = {
    "__pycache__", ".venv", ".git", "node_modules",
    "venv", "env", "dist", "build", ".next", ".cache",
    "target",
}
_HIDDEN_SUFFIXES = set()


class _AuraIconProvider(QFileIconProvider):
    """Custom icon provider that returns SVG icons for files and folders."""

    def __init__(self) -> None:
        super().__init__()
        self._file_icon = QIcon(str(media_path("file_24.svg")))
        self._folder_icon = QIcon(str(media_path("folder_24.svg")))

    def icon(self, arg):
        # QFileSystemModel calls icon(QFileInfo) for each item in the tree
        if isinstance(arg, QFileInfo):
            if arg.isDir():
                return self._folder_icon
            return self._file_icon
        # Standard icon-type fallback (e.g. for the header or other requests)
        if isinstance(arg, QFileIconProvider.IconType):
            if arg == QFileIconProvider.IconType.Folder:
                return self._folder_icon
            if arg == QFileIconProvider.IconType.File:
                return self._file_icon
            return super().icon(arg)
        return super().icon(arg)


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
            if model.fileInfo(idx).isSymLink():
                return False
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

    Read-only — activating a file asks the main window to open it in Aura.
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
        self._fs_model.setIconProvider(_AuraIconProvider())
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
            "QTreeView { border: none; background: transparent; } "
            "QTreeView::item { padding: 4px 6px; }"
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
        import time
        t0 = time.perf_counter()
        self._root = root
        src_index = self._fs_model.setRootPath(str(root))
        proxy_root = self._proxy.mapFromSource(src_index)
        self._view.setRootIndex(proxy_root)
        elapsed = time.perf_counter() - t0
        logging.getLogger(__name__).info("tree.set_root %s done in %.3fs", root, elapsed)

    def root(self) -> Path | None:
        return self._root

    # ---- handlers ---------------------------------------------------------

    def _on_double_clicked(self, proxy_index: QModelIndex) -> None:
        path = self._path_for(proxy_index)
        if path is None or path.is_dir():
            return
        self.file_activated.emit(path)

    def _on_context_menu(self, pos) -> None:
        proxy_index = self._view.indexAt(pos)
        if not proxy_index.isValid():
            return
        path = self._path_for(proxy_index)
        if path is None:
            return

        menu = QMenu(self)
        if path.is_file():
            open_aura = QAction("Open in Aura", menu)
            open_aura.triggered.connect(lambda: self.file_activated.emit(path))
            menu.addAction(open_aura)

            open_external = QAction("Open externally", menu)
            open_external.triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            )
            menu.addAction(open_external)
            menu.addSeparator()

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
                subprocess.run(
                    ["explorer", f"/select,{path}"],
                    check=False,
                    **get_subprocess_kwargs(),
                )
            except OSError:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        elif sys.platform == "darwin":
            subprocess.run(
                ["open", "-R", str(path)],
                check=False,
                **get_subprocess_kwargs(),
            )
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _copy_to_clipboard(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
