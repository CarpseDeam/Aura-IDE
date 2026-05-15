"""Card for showing code being written/edited in real time."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _HAVE_PYGMENTS, _fade_in_widget, _mono_font
from aura.gui.smooth_code_streamer import SmoothCodeStreamer
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

    # Animation timing / thresholds
    _ANIM_TICK_MS = 16          # ~60 fps
    _DELETE_CHARS_PER_TICK = 3
    _RETYPE_CHARS_PER_TICK = 5
    _INSTANT_TOTAL_CHARS = 5000
    _INSTANT_CHANGED_CHARS = 1500

    @staticmethod
    def _compute_changed_region(old_text: str, new_text: str) -> tuple[int, int, str, str]:
        """Return (prefix_len, suffix_len, old_middle, new_middle).

        Finds the longest common prefix and the longest common suffix that
        does not overlap the prefix, isolating the changed middle region.
        """
        prefix_len = 0
        while prefix_len < len(old_text) and prefix_len < len(new_text) and old_text[prefix_len] == new_text[prefix_len]:
            prefix_len += 1

        suffix_len = 0
        # Common suffix must not overlap the prefix region in either string
        while (suffix_len < len(old_text) - prefix_len
               and suffix_len < len(new_text) - prefix_len
               and old_text[len(old_text) - 1 - suffix_len] == new_text[len(new_text) - 1 - suffix_len]):
            suffix_len += 1

        old_middle = old_text[prefix_len:len(old_text) - suffix_len] if suffix_len else old_text[prefix_len:]
        new_middle = new_text[prefix_len:len(new_text) - suffix_len] if suffix_len else new_text[prefix_len:]

        return (prefix_len, suffix_len, old_middle, new_middle)

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setMinimumWidth(0)
        self._name = name
        self._path: str = ""
        self._state = self.STATE_RUNNING
        self._operation_count = 0
        self._completed_operations = 0
        self._active_operations = 0
        self._pending_content: str | None = None
        self._pending_state: str | None = None
        self._content_timer = QTimer(self)
        self._content_timer.setSingleShot(True)
        self._content_timer.setInterval(35)
        self._content_timer.timeout.connect(self._apply_pending_content)
        self._auto_size_timer = QTimer(self)
        self._auto_size_timer.setSingleShot(True)
        self._auto_size_timer.setInterval(100)
        self._auto_size_timer.timeout.connect(self._auto_size_code_view)

        # Animation state
        self._animating = False
        self._animation_target: str | None = None
        self._animation_phase: str = ""       # "delete" | "retype" | ""
        self._animation_prefix = ""
        self._animation_suffix = ""
        self._animation_old_middle = ""
        self._animation_new_middle = ""
        self._animation_char_index = 0

        self._animation_timer = QTimer(self)
        self._animation_timer.setSingleShot(False)
        self._animation_timer.setInterval(self._ANIM_TICK_MS)
        self._animation_timer.timeout.connect(self._tick_animation)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header
        self._header = QToolButton(self)
        self._header.setObjectName("sectionToggle")
        self._header.setMinimumWidth(0)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {FG_DIM}; }} "
            f"QToolButton#sectionToggle:hover {{ color: {FG}; }}"
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        # File path subtitle
        self._path_label = QLabel("", self)
        self._path_label.setMinimumWidth(0)
        self._path_label.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            "font-size: 10px;"
        )
        self._path_label.setVisible(False)
        body_layout.addWidget(self._path_label)

        # Code view
        self._code_view = QPlainTextEdit(self)
        self._code_view.setReadOnly(True)
        self._code_view.setMinimumWidth(0)
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
        self._language = "text"
        self._streamer = SmoothCodeStreamer(self._code_view, self)
        self._streamer.text_changed.connect(self._schedule_auto_size)
        self._streamer.finished.connect(self._on_streamer_finished)

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh_header()

        _fade_in_widget(self)

    def begin_update(self, name: str | None = None) -> None:
        """Mark that another write/edit operation is streaming into this card."""
        if name:
            self._name = name
        self._operation_count += 1
        self._active_operations += 1
        self._state = self.STATE_RUNNING
        self._refresh_header()

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "…",
            self.STATE_DONE: self._done_label(),
            self.STATE_FAILED: "Failed ✗",
        }[self._state]
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
        }[self._state]
        label = self._path if self._path else "Editing path…"
        prefix = f"{chev} 📝 "
        suffix = f"  {state_str}"
        metrics = QFontMetrics(self._header.font())
        available = max(40, self._header.width() - 10)
        label_width = max(24, available - metrics.horizontalAdvance(prefix + suffix))
        label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, label_width)
        text = f"{prefix}{label}{suffix}"
        self._header.setText(text)
        self._header.setToolTip(self._path or self._name)
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {state_color}; }}"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_header()

    def set_target_path(self, path: str) -> None:
        """Update path, labels, and highlighter from file extension."""
        self._path = path
        self._path_label.setText(f"📄 {path}")
        self._path_label.setVisible(True)
        self._refresh_header()

        # Update highlighter language from file extension
        if self._highlighter is not None and _HAVE_PYGMENTS:
            lang = language_from_path(path)
            if lang and lang != self._language:
                self._language = lang
                self._highlighter.set_language(lang)

    def update_content(self, content: str) -> None:
        """Update code content and adjust height."""
        self._pending_content = content
        self._content_timer.start()
        if not self._body.isVisible():
            self._body.setVisible(True)

    def show_content_transition(self, old_text: str, new_text: str) -> None:
        """Show a full-file old -> new transition."""
        if self._pending_content is not None:
            self._pending_content = None
            self._content_timer.stop()
        self._force_finish_animation()
        if self._streamer.visible_text() != old_text:
            self._apply_text_immediately(old_text)
        self._animate_to_content(old_text, new_text)

    def _set_code_text(self, text: str) -> None:
        """Set code view text without triggering auto-size (for animation frames)."""
        self._streamer.set_text_immediately(text)

    def _apply_text_immediately(self, text: str) -> None:
        """Instantly replace code view content and auto-size."""
        self._streamer.set_text_immediately(text)
        self._auto_size_code_view()

    def _apply_pending_content(self) -> None:
        """Apply the latest buffered content update with animation if appropriate."""
        if self._pending_content is None:
            return
        new_text = self._pending_content
        self._pending_content = None

        if self._animating:
            # Store as latest target; current animation will chain to it on finish
            self._animation_target = new_text
            return

        old_text = self._streamer.visible_text()
        self._animate_to_content(old_text, new_text)

    def _auto_size_code_view(self) -> None:
        doc = self._code_view.document()
        doc.setDocumentMargin(4)
        doc_height = doc.size().height() + 12
        # Start at 120 (approx 7-8 lines), max out at 600
        clamped = max(120, min(doc_height, 600))
        self._code_view.setFixedHeight(int(clamped))
        self.updateGeometry()

    def _schedule_auto_size(self) -> None:
        if not self._auto_size_timer.isActive():
            self._auto_size_timer.start()

    def _should_animate(self, old_text: str, new_text: str) -> bool:
        """Return True if delete/retype animation should be used.

        Skips animation for very large texts, empty old text (no prior
        content), or when the changed region is too large.
        """
        if len(old_text) > self._INSTANT_TOTAL_CHARS or len(new_text) > self._INSTANT_TOTAL_CHARS:
            return False
        if not old_text:
            return False
        if old_text == new_text:
            return False

        _prefix_len, _suffix_len, old_mid, new_mid = self._compute_changed_region(old_text, new_text)
        return max(len(old_mid), len(new_mid)) <= self._INSTANT_CHANGED_CHARS

    def _animate_to_content(self, old_text: str, new_text: str) -> None:
        """Animate from old_text to new_text, or instantly replace if animation is skipped."""
        if old_text == new_text:
            return
        if new_text.startswith(old_text):
            self._streamer.set_target(new_text)
            self._schedule_auto_size()
            return
        if not self._should_animate(old_text, new_text):
            self._apply_text_immediately(new_text)
            return
        self._start_animation(old_text, new_text)

    def _start_animation(self, old_text: str, new_text: str) -> None:
        """Begin the delete/retype animation from old_text to new_text."""
        prefix_len, suffix_len, old_mid, new_mid = self._compute_changed_region(old_text, new_text)

        old_end = len(old_text) - suffix_len if suffix_len > 0 else len(old_text)
        self._animation_prefix = new_text[:prefix_len]
        self._animation_suffix = (
            new_text[len(new_text) - suffix_len:] if suffix_len > 0 else ""
        )
        self._animation_old_middle = old_mid
        self._animation_new_middle = new_mid

        self._animation_target = None
        self._animating = True
        cursor = QTextCursor(self._code_view.document())
        text_len = max(0, self._code_view.document().characterCount() - 1)
        cursor.setPosition(max(0, min(prefix_len, text_len)))
        cursor.setPosition(
            max(0, min(old_end, text_len)),
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertText("")
        self._streamer.start_replacement(
            self._animation_prefix,
            self._animation_new_middle,
            self._animation_suffix,
            base_already_set=True,
        )

    def _tick_animation(self) -> None:
        """Process one animation frame (connected to _animation_timer.timeout)."""
        if self._animation_phase == "delete":
            self._animation_char_index = max(0, self._animation_char_index - self._DELETE_CHARS_PER_TICK)
            display = self._animation_prefix + self._animation_old_middle[:self._animation_char_index] + self._animation_suffix
            self._set_code_text(display)
            if self._animation_char_index == 0:
                # Transition to retype phase
                if self._animation_new_middle:
                    self._animation_phase = "retype"
                    self._animation_char_index = 0
                else:
                    self._finish_animation()
        elif self._animation_phase == "retype":
            self._animation_char_index = min(len(self._animation_new_middle),
                                             self._animation_char_index + self._RETYPE_CHARS_PER_TICK)
            display = self._animation_prefix + self._animation_new_middle[:self._animation_char_index] + self._animation_suffix
            self._set_code_text(display)
            if self._animation_char_index >= len(self._animation_new_middle):
                self._finish_animation()

    def _finish_animation(self) -> None:
        """Complete the current animation and chain to queued target if any."""
        self._animation_timer.stop()
        final_text = (
            self._animation_prefix + self._animation_new_middle + self._animation_suffix
        )
        next_target = self._animation_target
        self._animation_target = None
        self._animating = False

        if next_target is not None and next_target != final_text:
            self._animate_to_content(final_text, next_target)
        else:
            self._apply_text_immediately(final_text)

    def _force_finish_animation(self) -> None:
        """Immediately stop any running animation and show final content."""
        if not self._animating and not self._streamer.is_active():
            return
        self._animation_timer.stop()
        if not self._animating:
            self._streamer.finish(immediate=True)
            return
        final_text = (
            self._animation_prefix + self._animation_new_middle + self._animation_suffix
        )
        next_target = self._animation_target
        self._animation_target = None
        self._animating = False
        self._apply_text_immediately(next_target if next_target is not None else final_text)

    def set_result(self, ok: bool) -> None:
        if self._pending_content is not None:
            self._content_timer.stop()
            self._apply_pending_content()

        if ok:
            self._completed_operations += 1
        if self._active_operations > 0:
            self._active_operations -= 1

        if not ok:
            # Auto-expand body on failure
            self._force_finish_animation()
            self._body.setVisible(True)
            self._state = self.STATE_FAILED
        elif self._active_operations > 0:
            self._state = self.STATE_RUNNING
        elif self._streamer.is_active():
            self._pending_state = self.STATE_DONE
            self._streamer.finish()
        else:
            self._state = self.STATE_DONE
        self._refresh_header()
        if not self._streamer.is_active():
            self._auto_size_code_view()

    def _on_streamer_finished(self) -> None:
        self._animating = False
        if self._animation_target is not None:
            next_target = self._animation_target
            self._animation_target = None
            self._animate_to_content(self._streamer.visible_text(), next_target)
            return
        if self._pending_state is not None:
            self._state = self._pending_state
            self._pending_state = None
            self._refresh_header()
        self._auto_size_code_view()

    def _done_label(self) -> str:
        if self._completed_operations > 1:
            return f"{self._completed_operations} edits applied ✓"
        return "Applied ✓"
