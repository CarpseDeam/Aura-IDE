"""Shared UI components for the Aura glass theme."""
from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    QPoint,
    QPropertyAnimation,
    QTimer,
    QObject,
    QEvent
)
from PySide6.QtGui import (
    QFont
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QVBoxLayout, 
    QWidget, 
    QHBoxLayout, 
    QFrame, 
    QLabel,
    QApplication,
    QPlainTextEdit,
    QStackedWidget,
    QPushButton,
)

from aura.gui.theme import (
    ACCENT, 
    BG_RAISED, 
    BORDER, 
    FG, 
    FG_DIM, 
    BG, 
    SUCCESS, 
    WARN,
)
from aura.gui.syntax import PygmentsHighlighter, language_from_path as _language_from_path
from aura.resources import get_resource_path


# ===========================================================================
# Artifact/Worker Components (Moved from worker_window.py)
# ===========================================================================


_MERMAID_JS_PATH = get_resource_path("media/mermaid.min.js")
_MERMAID_JS: str = ""
try:
    _MERMAID_JS = _MERMAID_JS_PATH.read_text(encoding="utf-8")
except (FileNotFoundError, OSError):
    pass


def _is_previewable(language: str) -> bool:
    return language in ("html", "svg", "markdown", "mermaid")


class ArtifactCard(QFrame):
    """[DEPRECATED] Interactive card with Code/Preview toggle.

    Replaced by CodeEditorPane for the two-pane workspace.  Kept for
    backward compatibility with any external importers.
    """

    def __init__(self, artifact_id: str, label: str, language: str, content: str, parent=None):
        super().__init__(parent)
        self.setObjectName("artifactCard")
        self._artifact_id, self._label, self._language, self._content = artifact_id, label, language, content
        self._streaming = False
        self._typing_position = 0
        self._typing_timer = None
        self._typing_target = content

        self.setStyleSheet(f"QFrame#artifactCard {{ background: rgba(28, 28, 34, 0.5); border: 1px solid {BORDER}; border-radius: 10px; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(10, 6, 10, 6)
        
        self._header_label = QLabel(label, self)
        self._header_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        h_layout.addWidget(self._header_label)

        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        h_layout.addWidget(self._status_label)
        h_layout.addStretch(1)

        copy_btn = QPushButton("Copy", self)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self._content))
        h_layout.addWidget(copy_btn)

        if _is_previewable(language):
            self._toggle_btn = QPushButton("Preview", self)
            self._toggle_btn.clicked.connect(self._on_toggle_view)
            h_layout.addWidget(self._toggle_btn)
        
        outer.addWidget(header)

        self._stack = QStackedWidget(self)
        self._code_view = QPlainTextEdit(self)
        self._code_view.setReadOnly(True)
        self._code_view.setFont(QFont("Geist Mono", 9))
        self._code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._code_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._code_view.setStyleSheet(f"background: {BG}; border: none; padding: 8px;")
        self._stack.addWidget(self._code_view)

        self._highlighter = PygmentsHighlighter(self._code_view.document(), language) if _language_from_path else None

        self._preview_view = QWebEngineView(self)
        self._stack.addWidget(self._preview_view)
        outer.addWidget(self._stack)

        self._refresh_code_view()
        self._refresh_preview()

    def _on_toggle_view(self):
        idx = 1 - self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)
        self._toggle_btn.setText("Code" if idx == 1 else "Preview")
        if idx == 1:
            self._refresh_preview()

    def set_target_path(self, path: str):
        self._label = path
        self._header_label.setText(self._label)
        self._language = _language_from_path(path)
        if self._highlighter:
            self._highlighter.deleteLater()
        self._highlighter = PygmentsHighlighter(self._code_view.document(), self._language)

    def update_content(self, content: str):
        self._content = content
        if self._streaming:
            self._start_typing(content)
        else:
            self._refresh_code_view()
        self._refresh_preview()
        self.updateGeometry()

    def set_streaming(self, active: bool):
        self._streaming = active
        self._status_label.setText("● streaming" if active else "✓ done")
        if not active:
            self._flush_typing()

    def _start_typing(self, target: str):
        if not self._typing_timer:
            self._typing_timer = QTimer(self)
            self._typing_timer.timeout.connect(self._on_typing_tick)
        self._typing_target = target
        if self._typing_position > len(target):
            self._typing_position = 0
        if not self._typing_timer.isActive():
            self._typing_timer.start(33)

    def _on_typing_tick(self):
        if self._typing_position >= len(self._typing_target):
            self._typing_timer.stop()
            return
        self._typing_position += 5
        self._code_view.setPlainText(self._typing_target[:self._typing_position])
        self._auto_size()
        # Auto-scroll to bottom
        sb = self._code_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _flush_typing(self):
        if self._typing_timer:
            self._typing_timer.stop()
        self._typing_position = len(self._content)
        self._refresh_code_view()

    def _auto_size(self):
        h = self._code_view.document().size().height() + 20
        height = int(max(120, min(h, 600)))
        self._code_view.setFixedHeight(height)
        self._preview_view.setFixedHeight(height)
        self._stack.setFixedHeight(height)

    def _refresh_code_view(self):
        self._code_view.setPlainText(self._content)
        self._auto_size()
        # Auto-scroll to bottom
        sb = self._code_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_preview(self):
        if self._language == "html":
            self._preview_view.setHtml(self._content)
        elif self._language == "mermaid":
            mermaid_include = f"<script>{_MERMAID_JS}</script>" if _MERMAID_JS else ""
            html = f"<html><body>{mermaid_include}<div class='mermaid'>{self._content}</div><script>mermaid.initialize({{startOnLoad:true}})</script></body></html>"
            self._preview_view.setHtml(html)


class WorkerLogCard(QFrame):
    """[DEPRECATED] Card for typewriter worker activity log.

    Replaced by InfoHubPane for the two-pane workspace.  Kept for
    backward compatibility with any external importers.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workerLogCard")
        self.setStyleSheet(f"QFrame#workerLogCard {{ background: rgba(28, 28, 34, 0.4); border: 1px solid {BORDER}; border-radius: 8px; }}")
        layout = QVBoxLayout(self)
        self._header = QLabel("⚡ Worker Activity", self)
        self._header.setStyleSheet(f"color: {ACCENT}; font-weight: 700;")
        layout.addWidget(self._header)
        self._content_view = QPlainTextEdit(self)
        self._content_view.setReadOnly(True)
        self._content_view.setFont(QFont("Geist Mono", 10))
        self._content_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._content_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_view.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._content_view)
        self._full, self._visible, self._timer = "", "", QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.setInterval(20)

    def append_text(self, text: str, is_reasoning=False):
        self._full += text
        if not self._timer.isActive():
            self._timer.start()

    def _on_tick(self):
        if len(self._visible) >= len(self._full):
            self._timer.stop()
            return
        self._visible += self._full[len(self._visible):len(self._visible)+2]
        self._content_view.setPlainText(self._visible)
        h = self._content_view.document().size().height() + 15
        self._content_view.setFixedHeight(int(max(120, min(h, 600))))
        # Auto-scroll to bottom
        sb = self._content_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self):
        self._timer.stop()
        self._full = ""
        self._visible = ""
        self._content_view.setPlainText("")


from aura.gui.playground import AuraPlayground  # noqa: F401

from aura.gui.widgets.glass_switch import GlassSwitch  # noqa: F401, E402
from aura.gui.widgets.aura_glow import AuraWidget  # noqa: F401
from aura.gui.widgets.todo_list import TodoListWidget  # noqa: F401
