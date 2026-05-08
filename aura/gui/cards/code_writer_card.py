"""Card for showing code being written/edited in real time."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget

from aura.gui.cards._helpers import _HAVE_PYGMENTS, _fade_in_widget, _mono_font
from aura.gui.syntax import PygmentsHighlighter, language_from_path
from aura.gui.theme import BG, BORDER, DANGER, FG, FG_DIM, SUCCESS, WARN


class CodeWriterCard(QFrame):
    """Card for showing code being written/edited in real time.

    Header: "📝 Writing code…" with collapsible toggle.
    Body: file path label + monospace code view that streams character-by-character.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName("toolCard")
        self._name = name
        self._path: str = ""
        self._state = self.STATE_RUNNING

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header
        self._header = QToolButton()
        self._header.setObjectName("sectionToggle")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {FG_DIM}; }} "
            f"QToolButton#sectionToggle:hover {{ color: {FG}; }}"
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        # File path subtitle
        self._path_label = QLabel("")
        self._path_label.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            "font-size: 10px;"
        )
        self._path_label.setVisible(False)
        body_layout.addWidget(self._path_label)

        # Code view
        self._code_view = QPlainTextEdit()
        self._code_view.setReadOnly(True)
        self._code_view.setFont(_mono_font(10))
        self._code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._code_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; }}"
        )
        body_layout.addWidget(self._code_view)

        # Native syntax highlighter (language will be updated when path is known)
        self._highlighter: PygmentsHighlighter | None = None
        if _HAVE_PYGMENTS:
            self._highlighter = PygmentsHighlighter(self._code_view.document(), "text")

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh_header()

        _fade_in_widget(self)

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "…",
            self.STATE_DONE: "Applied ✓",
            self.STATE_FAILED: "Failed ✗",
        }[self._state]
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
        }[self._state]
        label = self._path if self._path else "Writing code…"
        text = f"{chev} 📝 {label}  {state_str}"
        self._header.setText(text)
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {state_color}; }}"
        )

    def set_target_path(self, path: str) -> None:
        """Update path, labels, and highlighter from file extension."""
        self._path = path
        self._path_label.setText(f"📄 {path}")
        self._path_label.setVisible(True)
        self._refresh_header()

        # Update highlighter language from file extension
        if self._highlighter is not None and _HAVE_PYGMENTS:
            lang = language_from_path(path)
            if lang:
                self._highlighter.set_language(lang)

    def update_content(self, content: str) -> None:
        """Update code content and adjust height."""
        self._code_view.setPlainText(content)
        self._auto_size_code_view()
        if not self._body.isVisible():
            self._body.setVisible(True)

    def _auto_size_code_view(self) -> None:
        doc = self._code_view.document()
        doc.setDocumentMargin(4)
        doc_height = doc.size().height() + 8  # small padding
        doc_height = max(60, min(doc_height, 400))  # clamp between 60-400
        self._code_view.setFixedHeight(int(doc_height))

    def set_result(self, ok: bool) -> None:
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        if not ok:
            # Auto-expand body on failure
            self._body.setVisible(True)
        self._refresh_header()
