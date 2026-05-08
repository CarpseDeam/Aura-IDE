"""Embeddable panel for worker dispatch output.

Shows a pinned TODO list and interactive artifact cards (HTML, SVG,
Markdown, Mermaid) with code/preview toggle and QWebEngineView preview.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aura.gui.aura_widget import AuraWidget
from aura.gui.theme import BG, BORDER, FG, FG_DIM, SUCCESS, WARN
from aura.gui.syntax import PygmentsHighlighter, language_from_path as _language_from_path

_HAVE_PYGMENTS = True

# ---------------------------------------------------------------------------
# Load mermaid.min.js at module init time so we can embed it in preview HTML.
# ---------------------------------------------------------------------------

_MERMAID_JS_PATH = Path(__file__).resolve().parent.parent.parent / "media" / "mermaid.min.js"

_MERMAID_JS: str = ""
try:
    _MERMAID_JS = _MERMAID_JS_PATH.read_text(encoding="utf-8")
except (FileNotFoundError, OSError):
    pass  # Fall back to CDN in the HTML template.


def _is_previewable(language: str) -> bool:
    """Whether this language supports a rendered Preview toggle."""
    return language in ("html", "svg", "markdown", "mermaid")


# ===========================================================================
# TodoListWidget — kept exactly as-is from the original worker_window.py
# ===========================================================================


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan with live status updates."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("todoListWidget")
        self.setStyleSheet(
            f"QFrame#todoListWidget {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"  padding: 0;"
            f"}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(4)

        # Header
        header = QLabel("TODO LIST")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        # Container for task labels
        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self._pulse_anims: list = []  # QVariantAnimation list

        self.setVisible(False)  # Hidden until tasks arrive

    def update_tasks(self, tasks: list[dict]) -> None:
        """Clear and redraw the task list from the worker's update_todo_list tool."""
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        # Stop any running pulse animations
        for anim in self._pulse_anims:
            anim.stop()
            anim.deleteLater()
        self._pulse_anims.clear()

        # Remove old task labels
        while self._tasks_layout.count() > 0:
            item = self._tasks_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if not tasks:
            self.setVisible(False)
            return

        self.setVisible(True)

        for task in tasks:
            description = task.get("description", "")
            status = task.get("status", "pending")

            # Choose prefix and color
            if status == "done":
                prefix = "✓"
                color = SUCCESS
            elif status == "active":
                prefix = "►"
                color = WARN
            else:  # pending
                prefix = "○"
                color = FG_DIM

            label_text = f"{prefix} {description}"
            label = QLabel(label_text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            # Monospace font
            font = label.font()
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPointSize(11)
            label.setFont(font)

            # Bold for active tasks
            if status == "active":
                font.setBold(True)
                label.setFont(font)

                # Add a breathing pulse animation to the label
                effect = QGraphicsOpacityEffect(label)
                effect.setOpacity(1.0)
                label.setGraphicsEffect(effect)

                pulse = QVariantAnimation(label)
                pulse.setStartValue(0.55)
                pulse.setEndValue(1.0)
                pulse.setDuration(900)
                pulse.setLoopCount(-1)
                pulse.setEasingCurve(QEasingCurve.Type.InOutSine)

                def _make_opacity_setter(eff):
                    return lambda v: eff.setOpacity(v)

                pulse.valueChanged.connect(_make_opacity_setter(effect))
                pulse.start()
                self._pulse_anims.append(pulse)

            label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            self._tasks_layout.addWidget(label)


# ===========================================================================
# ArtifactCard
# ===========================================================================


class ArtifactCard(QFrame):
    """Interactive card with a header bar and QStackedWidget toggling
    between Code View (syntax-highlighted) and Preview View (QWebEngineView).

    Supports HTML, SVG, Markdown, and Mermaid content.
    """

    def __init__(
        self,
        artifact_id: str,
        label: str,
        language: str,
        content: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("artifactCard")
        self._artifact_id = artifact_id
        self._label = label
        self._language = language
        self._content = content

        # Streaming state
        self._streaming = False
        self._streaming_cursor = None  # QTimer for blink
        self._cursor_label = None  # QLabel for "⏳ streaming..." indicator
        self._edit_old_str: str = ""
        self._edit_new_str: str = ""

        # Typing effect state
        self._typing_timer = None  # QTimer, created lazily
        self._typing_target = ""   # full text to reveal
        self._typing_position = 0  # how many chars revealed so far

        # Glass-card styling (same as old CodeStreamCard)
        self.setStyleSheet("""
            QFrame#artifactCard {
                background: rgba(28, 28, 34, 0.50);
                border-top: 1px solid rgba(255, 255, 255, 0.06);
                border-right: 1px solid rgba(0, 0, 0, 0.18);
                border-bottom: 1px solid rgba(0, 0, 0, 0.25);
                border-left: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 10px;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Header bar ---------------------------------------------------
        header = QWidget()
        header.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        # Icon label
        icon_lbl = QLabel("< >")
        icon_lbl.setStyleSheet(f"color: {FG_DIM}; font-family: 'Geist Mono', monospace; font-size: 11px;")
        header_layout.addWidget(icon_lbl)

        # Language / filename label
        self._header_label = QLabel(label)
        self._header_label.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 12px;")
        header_layout.addWidget(self._header_label)

        # Status indicator (streaming / done / edit phase)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        header_layout.addWidget(self._status_label)

        header_layout.addStretch(1)

        # Copy button
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedHeight(24)
        copy_btn.setStyleSheet(
            f"QPushButton {{ color: {FG_DIM}; background: transparent; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 2px 8px; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {FG}; border-color: {FG_DIM}; }}"
        )
        copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(copy_btn)

        # Download button
        download_btn = QPushButton("Download")
        download_btn.setFixedHeight(24)
        download_btn.setStyleSheet(
            f"QPushButton {{ color: {FG_DIM}; background: transparent; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 2px 8px; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {FG}; border-color: {FG_DIM}; }}"
        )
        download_btn.clicked.connect(self._on_download)
        header_layout.addWidget(download_btn)

        # Expand/collapse button
        self._expand_btn = QPushButton("Collapse")
        self._expand_btn.setFixedHeight(24)
        self._expand_btn.setStyleSheet(
            f"QPushButton {{ color: {FG_DIM}; background: transparent; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 2px 8px; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {FG}; border-color: {FG_DIM}; }}"
        )
        self._expand_btn.clicked.connect(self._on_expand_toggle)
        header_layout.addWidget(self._expand_btn)

        # Toggle View button (Code / Preview) — only for previewable types
        if _is_previewable(language):
            self._toggle_btn = QPushButton("Preview")
            self._toggle_btn.setFixedHeight(24)
            self._toggle_btn.setStyleSheet(
                f"QPushButton {{ color: {FG_DIM}; background: transparent; "
                f"border: 1px solid {BORDER}; border-radius: 4px; "
                f"padding: 2px 8px; font-size: 10px; }}"
                f"QPushButton:hover {{ color: {FG}; border-color: {FG_DIM}; }}"
            )
            self._toggle_btn.clicked.connect(self._on_toggle_view)
            header_layout.addWidget(self._toggle_btn)
        else:
            self._toggle_btn = None  # No preview for code-only files

        outer.addWidget(header)

        # ---- Body: QStackedWidget -----------------------------------------
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")

        # Page 0 — Code View
        self._code_view = QPlainTextEdit()
        self._code_view.setReadOnly(True)
        font = QFont("Geist Mono, JetBrains Mono, Consolas, monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self._code_view.setFont(font)
        self._code_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; "
            f"border: none; padding: 8px; }}"
        )
        self._code_view.setMinimumHeight(60)
        self._code_view.setMaximumHeight(400)
        self._stack.addWidget(self._code_view)  # index 0

        # Attach native syntax highlighter
        if _HAVE_PYGMENTS:
            PygmentsHighlighter(self._code_view.document(), language)

        # Page 1 — Preview View (QWebEngineView)
        self._preview_view = QWebEngineView()
        self._preview_view.setMinimumHeight(100)
        self._preview_view.setStyleSheet("background: transparent; border: none;")
        self._stack.addWidget(self._preview_view)  # index 1

        self._stack.setCurrentIndex(0)  # Start in code view
        outer.addWidget(self._stack, 1)

        # Populate initial content
        self._refresh_code_view()
        self._refresh_preview()

    # ---- Properties -------------------------------------------------------

    @property
    def language(self) -> str:
        return self._language

    @property
    def artifact_id(self) -> str:
        return self._artifact_id

    # ---- Public API -------------------------------------------------------

    def update_content(self, content: str) -> None:
        """Update internal content, refresh code view and preview."""
        self._content = content
        if self._streaming:
            # In streaming mode, animate the content character by character
            self._start_typing(content)
        else:
            self._refresh_code_view()
        self._refresh_preview()

    def set_streaming(self, active: bool) -> None:
        """Show/hide a streaming indicator on the card."""
        self._streaming = active
        if active:
            self._status_label.setText("● streaming")
            # Start a subtle pulse timer
            if self._streaming_cursor is None:
                from PySide6.QtCore import QTimer
                self._streaming_cursor = QTimer(self)
                self._streaming_cursor.timeout.connect(self._toggle_status_dot)
                self._streaming_cursor.start(600)
        else:
            if self._streaming_cursor is not None:
                self._streaming_cursor.stop()
                self._streaming_cursor = None
            self._flush_typing()
            self._status_label.setText("✓ done")
            # Fade the done indicator after 2 seconds
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, lambda: self._status_label.setText(""))

    def _toggle_status_dot(self) -> None:
        if self._status_label.text() == "● streaming":
            self._status_label.setText("○ streaming")
        else:
            self._status_label.setText("● streaming")

    def set_edit_phase(self, phase: str) -> None:
        """For edit_file: 'old' = showing old_str, 'new' = streaming new_str."""
        if phase == "old":
            self._status_label.setText("✂ removing...")
        elif phase == "new":
            self._typing_position = 0
            self._status_label.setText("● streaming")

    def _start_typing(self, target: str) -> None:
        """Begin or continue the typing animation toward `target`."""
        from PySide6.QtCore import QTimer

        self._typing_target = target
        # If position is already past the new target (edit_file transition),
        # reset to animate the new content from scratch.
        if self._typing_position > len(target):
            self._typing_position = 0

        if self._typing_timer is None:
            self._typing_timer = QTimer(self)
            self._typing_timer.timeout.connect(self._on_typing_tick)
            self._typing_timer.setInterval(33)  # ~30 fps
        if not self._typing_timer.isActive():
            self._typing_timer.start()

    def _on_typing_tick(self) -> None:
        """Reveal a chunk of characters from the typing buffer."""
        if self._typing_position >= len(self._typing_target):
            self._typing_timer.stop()
            return

        # Reveal ~3-5 characters per tick for a smooth but fast effect
        remaining = len(self._typing_target) - self._typing_position
        chunk = max(1, min(remaining, 5))
        self._typing_position += chunk
        partial = self._typing_target[:self._typing_position]

        # Native highlighter applies colors automatically — no HTML needed
        self._code_view.setPlainText(partial)

        # Auto-scroll to bottom
        sb = self._code_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _flush_typing(self) -> None:
        """Immediately reveal all remaining typing content."""
        if self._typing_timer is not None:
            self._typing_timer.stop()
        self._typing_position = len(self._typing_target)
        self._refresh_code_view()

    # ---- Button handlers --------------------------------------------------

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._content)

    def _on_download(self) -> None:
        ext_map = {
            "html": "HTML Files (*.html)",
            "svg": "SVG Files (*.svg)",
            "markdown": "Markdown Files (*.md)",
            "mermaid": "Mermaid Files (*.mmd)",
        }
        file_filter = ext_map.get(self._language, "All Files (*.*)")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save Artifact", self._label, file_filter
        )
        if chosen:
            Path(chosen).write_text(self._content, encoding="utf-8")

    def _on_expand_toggle(self) -> None:
        visible = self._stack.isVisible()
        self._stack.setVisible(not visible)
        self._expand_btn.setText("Expand" if visible else "Collapse")

    def _on_toggle_view(self) -> None:
        if self._toggle_btn is None:
            return
        current = self._stack.currentIndex()
        if current == 0:
            self._stack.setCurrentIndex(1)
            self._toggle_btn.setText("Code")
            self._refresh_preview()
        else:
            self._stack.setCurrentIndex(0)
            self._toggle_btn.setText("Preview")

    # ---- Internal refresh helpers -----------------------------------------

    def _refresh_code_view(self) -> None:
        """Update the code view with current content."""
        self._code_view.setPlainText(self._content)

    def _refresh_preview(self) -> None:
        """Render the preview in the QWebEngineView based on language."""
        if self._language == "html":
            self._preview_view.setHtml(self._content)
        elif self._language == "svg":
            wrapped = (
                f'<html><body style="margin:0;background:#{BG};">'
                f"{self._content}</body></html>"
            )
            self._preview_view.setHtml(wrapped)
        elif self._language == "markdown":
            from PySide6.QtGui import QTextDocument

            doc = QTextDocument()
            doc.setMarkdown(self._content)
            body_html = doc.toHtml()
            wrapped = (
                "<html><body style=\"margin:12px; background:#ffffff; "
                "color:#1a1a1a; font-family:'Segoe UI',sans-serif; "
                "line-height:1.6;\">"
                f"{body_html}</body></html>"
            )
            self._preview_view.setHtml(wrapped)
        elif self._language == "mermaid":
            if _MERMAID_JS:
                mermaid_include = f"<script>{_MERMAID_JS}</script>"
            else:
                mermaid_include = (
                    '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js">'
                    "</script>"
                )
            html = (
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                f"{mermaid_include}"
                "</head><body>"
                f'<div class="mermaid">{self._content}</div>'
                "<script>mermaid.initialize({startOnLoad:true, theme:'default'})</script>"
                "</body></html>"
            )
            self._preview_view.setHtml(html)


# ===========================================================================
# AuraPlayground — replaces WorkerWindow
# ===========================================================================


class AuraPlayground(QWidget):
    """Right-side panel showing TODO list and ArtifactCards for worker output.

    Public API matches the old WorkerWindow so main_window.py only needs
    import/name changes.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Playground")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 8px 12px;")
        layout.addWidget(header)

        # Pinned TODO list
        self._todo_widget = TodoListWidget()
        layout.addWidget(self._todo_widget)

        # Scrollable container for artifact cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._card_container = QWidget()
        self._card_container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(8, 0, 8, 0)
        self._card_layout.setSpacing(24)
        self._card_layout.addStretch(1)  # push cards to top
        scroll.setWidget(self._card_container)
        self._scroll = scroll

        layout.addWidget(scroll, 1)

        # Internal state
        self._artifacts: dict[str, ArtifactCard] = {}
        self._write_tools: dict[str, dict] = {}
        self._artifact_counter = 0
        self._auras: dict[str, AuraWidget] = {}
    # ---- helpers -----------------------------------------------------------

    def _add_artifact(
        self, artifact_id: str, label: str, language: str, content: str
    ) -> ArtifactCard:
        """Create card, add to layout, store in dict, return it."""
        card = ArtifactCard(artifact_id, label, language, content)
        self._artifacts[artifact_id] = card
        # Wrap in AuraWidget for breathing glow effect
        wrapper = AuraWidget(card, glow_color="#7aa2f7", glow_spread=20)
        self._auras[artifact_id] = wrapper
        # Insert before the trailing stretch
        self._card_layout.insertWidget(self._card_layout.count() - 1, wrapper)
        self._scroll_to_bottom()
        return card

    def _scroll_to_bottom(self) -> None:
        """Scroll the outer scroll area to the bottom."""
        bar = self._scroll.verticalScrollBar()
        if bar:
            bar.setValue(bar.maximum())

    # ---- public streaming API (matches old WorkerWindow) ------------------

    def begin_assistant(self) -> None:
        """Clear all existing artifact cards. Do NOT clear the TODO widget."""
        for card in list(self._artifacts.values()):
            card.deleteLater()
        self._artifacts.clear()
        self._auras.clear()
        self._write_tools.clear()
        self._artifact_counter = 0

    def append_reasoning(self, text: str) -> None:
        """No-op."""

    def append_content(self, text: str) -> None:
        """No-op."""

    def add_tool_call(self, worker_tool_id: str, name: str) -> None:
        """Track write_file / edit_file calls for artifact streaming."""
        if name in ("write_file", "edit_file"):
            self._write_tools[worker_tool_id] = {
                "name": name,
                "buffered_args": "",
                "last_content_len": 0,
                "path": "",
                "content": "",
            }

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        """Stream JSON args for write_file/edit_file, creating/updating ArtifactCards.

        Supports ALL file types (not just .html/.svg/.md). For edit_file,
        shows old_str first then transitions to streaming new_str.
        """
        info = self._write_tools.get(worker_tool_id)
        if info is None:
            return

        info["buffered_args"] += fragment

        try:
            parsed = json.loads(info["buffered_args"])
        except json.JSONDecodeError:
            # Try regex to extract path from partial JSON
            m = re.search(r'"path"\s*:\s*"([^"]*)', info["buffered_args"])
            if m and not info["path"]:
                info["path"] = m.group(1)
                # Create a placeholder card as soon as we know the path
                artifact_id = f"file-{worker_tool_id}"
                if artifact_id not in self._artifacts:
                    language = _language_from_path(info["path"])
                    label = Path(info["path"]).name
                    self._add_artifact(artifact_id, label, language, "")
            # Also try to extract old_str from partial JSON for edit_file
            m_old = re.search(r'"old_str"\s*:\s*"', info["buffered_args"])
            if m_old and "old_str_seen" not in info:
                info["old_str_seen"] = True
            return

        # Successfully parsed full JSON
        path = parsed.get("path", "")
        if path:
            info["path"] = path

        artifact_id = f"file-{worker_tool_id}"
        language = _language_from_path(path) if path else "text"
        label = Path(path).name if path else info["name"]

        if info["name"] == "write_file":
            content = parsed.get("content", "")
            info["content"] = content
            if artifact_id not in self._artifacts:
                card = self._add_artifact(artifact_id, label, language, content)
                card.set_streaming(True)
                aura = self._auras.get(artifact_id)
                if aura is not None:
                    aura.start_aura()
            else:
                self._artifacts[artifact_id].update_content(content)

        elif info["name"] == "edit_file":
            old_str = parsed.get("old_str", "")
            new_str = parsed.get("new_str", "")
            if artifact_id not in self._artifacts:
                # Start by showing old_str
                card = self._add_artifact(artifact_id, label, language, old_str if old_str else "")
                card.set_edit_phase("old")
                card._edit_old_str = old_str
                card._edit_new_str = ""
            card = self._artifacts[artifact_id]
            if new_str and not card._edit_new_str:
                # First chunk of new_str arrived — transition to showing new_str
                card.set_edit_phase("new")
                card.set_streaming(True)
                aura = self._auras.get(artifact_id)
                if aura is not None:
                    aura.set_glow_state("coding")
                    aura.start_aura()
            if new_str:
                card._edit_new_str = new_str
                card.update_content(new_str)

        self._scroll_to_bottom()

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str) -> None:
        """Finalize streaming indicator; keep card on success, remove on failure."""
        info = self._write_tools.pop(worker_tool_id, None)
        if info is None:
            return

        artifact_id = f"file-{worker_tool_id}"
        card = self._artifacts.get(artifact_id)
        if card is None:
            return

        card.set_streaming(False)  # stop the pulse

        aura = self._auras.get(artifact_id)
        if aura is not None:
            aura.stop_aura()

        if ok:
            # Keep the card — it shows the final content
            pass
        else:
            # On failure, remove the card (the change wasn't applied)
            card.deleteLater()
            del self._artifacts[artifact_id]

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        """No-op."""

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        """No-op."""

    def add_error(self, message: str) -> None:
        """No-op."""

    def worker_finished(self, ok: bool, summary: str) -> None:
        """Artifact cards stay visible — no-op."""

    def worker_cancelled(self) -> None:
        """No-op."""

    def update_todo_list(self, tasks: list) -> None:
        """Forward the worker's TODO list update to the pinned widget."""
        self._todo_widget.update_tasks(tasks)

    def clear(self) -> None:
        """Remove all artifact cards, reset state, clear TODO widget."""
        for card in list(self._artifacts.values()):
            card.deleteLater()
        self._artifacts.clear()
        self._auras.clear()
        self._write_tools.clear()
        self._artifact_counter = 0
        self._todo_widget.update_tasks([])

    # ---- New methods -------------------------------------------------------

    def add_mermaid_artifact(self, code: str) -> None:
        """Called when the planner generates a mermaid diagram."""
        artifact_id = f"mermaid-{self._artifact_counter}"
        self._artifact_counter += 1
        self._add_artifact(artifact_id, "Mermaid Diagram", "mermaid", code)
