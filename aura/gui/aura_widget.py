"""Shared UI components for the Aura glass theme."""
from __future__ import annotations

import math

from PySide6.QtCore import (
    QAbstractAnimation, 
    QEasingCurve, 
    QRectF, 
    QVariantAnimation, 
    Signal, 
    Qt, 
    QPoint, 
    QPropertyAnimation,
    QTimer,
    QObject,
    QEvent
)
from PySide6.QtGui import (
    QColor, 
    QPainter, 
    QPainterPath, 
    QRadialGradient,
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
    QFileDialog,
    QGraphicsOpacityEffect,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QPushButton
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
    DANGER
)
from aura.gui.controllers import ToolStreamController
from aura.gui.syntax import PygmentsHighlighter, language_from_path as _language_from_path


# ===========================================================================
# GlassSwitch
# ===========================================================================


class GlassSwitch(QWidget):
    """Custom toggle switch that fits the Aura glass theme."""
    toggled = Signal(bool)

    def __init__(self, label: str, checked: bool = False, vertical: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._checked = checked
        self._label_text = label
        self._label: QLabel | None = None
        
        if vertical:
            layout = QVBoxLayout(self)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            layout = QHBoxLayout(self)
        
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # The Track
        self._track = QFrame()
        self._track.setFixedSize(32, 16)
        self._track.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track.setStyleSheet(self._get_track_style())
        
        # The Thumb
        self._thumb = QFrame(self._track)
        self._thumb.setFixedSize(10, 10)
        self._thumb.move(3 if not checked else 19, 3)
        self._thumb.setStyleSheet(
            f"background: {FG}; border-radius: 5px;"
        )
        self._thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout.addWidget(self._track, 0, Qt.AlignmentFlag.AlignCenter)
        self._track.installEventFilter(self)
        
        if label:
            self._label = QLabel(label)
            self._label.setCursor(Qt.CursorShape.PointingHandCursor)
            self._label.installEventFilter(self)
            layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)
            self._refresh_label_style()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self.setChecked(not self._checked)
                self.toggled.emit(self._checked)
                return True
        return super().eventFilter(watched, event)

    def _get_track_style(self) -> str:
        bg = ACCENT if self._checked else BG_RAISED
        border = ACCENT if self._checked else BORDER
        return f"background: {bg}; border: 1px solid {border}; border-radius: 8px;"

    def _refresh_label_style(self) -> None:
        if self._label is None:
            return
        color = ACCENT if self._checked else FG_DIM
        weight = 700 if self._checked else 600
        self._label.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: {weight};"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def setChecked(self, checked: bool):
        self._checked = checked
        self._track.setStyleSheet(self._get_track_style())
        self._refresh_label_style()
        # Animate thumb position
        self._anim = QPropertyAnimation(self._thumb, b"pos")
        self._anim.setDuration(120)
        self._anim.setEndValue(QPoint(19 if checked else 3, 3))
        self._anim.start()

    def isChecked(self) -> bool:
        return self._checked


# ===========================================================================
# AuraWidget (Breathing Glow)
# ===========================================================================


class AuraWidget(QWidget):
    """Wrapper widget that draws a soft breathing radial glow underneath an inner card.

    The glow pulsates: it expands outward and fades in, then contracts and fades out,
    creating a low-key neon-light effect beneath the card.
    """

    def __init__(
        self,
        inner_widget: QWidget,
        glow_color: str = "#6d28d9",
        glow_spread: int = 20,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glow_color = QColor(glow_color)
        self._glow_spread = glow_spread
        self._breath: float = 0.0
        self._cached_ring_path: QPainterPath | None = None

        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            glow_spread, glow_spread, glow_spread, glow_spread,
        )
        layout.addWidget(inner_widget)

        # Breathing animation: cycles 0.0 -> 1.0 infinitely
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(2000)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._on_breath_changed)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        rect = self.rect()
        if rect.isEmpty():
            self._cached_ring_path = None
            return

        s = self._glow_spread
        outer_rect = QRectF(rect)
        inner_rect = QRectF(
            rect.x() + s, rect.y() + s,
            rect.width() - 2 * s, rect.height() - 2 * s,
        )
        outer_path = QPainterPath()
        # Keeping 8px radius to match "just like it is now"
        outer_path.addRoundedRect(outer_rect, 8, 8)
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner_rect, 8, 8)
        self._cached_ring_path = outer_path.subtracted(inner_path)

    def _on_breath_changed(self, value: float) -> None:
        # Sine shaping: 0 -> 1 -> 0 for smooth breathe-in / breathe-out
        self._breath = math.sin(value * math.pi)
        self.update()

    def start_aura(self) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            self._animation.start()

    def stop_aura(self) -> None:
        self._animation.stop()
        self._breath = 0.0
        self.update()

    def transition_glow_color(self, new_color: str, duration: int = 600) -> None:
        """Animate the glow color from its current value to *new_color*."""
        target = QColor(new_color)
        start = QColor(self._glow_color)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        def _on_value(v: float) -> None:
            r = int(start.red() + (target.red() - start.red()) * v)
            g = int(start.green() + (target.green() - start.green()) * v)
            b = int(start.blue() + (target.blue() - start.blue()) * v)
            a = int(start.alpha() + (target.alpha() - start.alpha()) * v)
            self._glow_color = QColor(r, g, b, a)
            self.update()
        anim.valueChanged.connect(_on_value)
        anim.start()

    def set_glow_state(self, state: str) -> None:
        """Transition the glow to a semantic colour state."""
        colors = {
            "thinking": "#9b30ff",
            "coding": "#00e5ff",
        }
        color = colors.get(state)
        if color is not None:
            self.transition_glow_color(color)
            self.start_aura()

    def paintEvent(self, event) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            # No glow when idle - fully transparent
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        if rect.isEmpty():
            painter.end()
            return

        b = self._breath
        if b < 0.005:
            painter.end()
            return

        # Build a ring-shaped clip path: outer rounded rect minus inner
        # rounded rect, so the glow only appears in the margin around the card.
        if self._cached_ring_path:
            painter.setClipPath(self._cached_ring_path)

        # Radial gradient centered on the widget - still pulses with breath
        center = rect.center()
        max_r = max(rect.width(), rect.height()) * 0.5
        radius = max_r * (0.3 + 0.7 * b)  # expands/contracts with breath

        alpha = int(220 * b)  # fades in/out with breath

        c = self._glow_color
        inner = QColor(c.red(), c.green(), c.blue(), alpha)
        mid = QColor(c.red(), c.green(), c.blue(), alpha // 2)
        outer = QColor(0, 0, 0, 0)

        gradient = QRadialGradient(center, radius)
        # Position the strongest colour at the inner edge of the ring so the
        # glow fades naturally from the card border outward.
        inner_pos = 0.0
        s = self._glow_spread
        if max_r > 0:
            inner_pos = max(0.0, (min(rect.width(), rect.height()) * 0.5 - s) / max_r)
        inner_pos = min(inner_pos, 0.99)
        gradient.setColorAt(inner_pos, inner)
        gradient.setColorAt(inner_pos + (1.0 - inner_pos) * 0.5, mid)
        gradient.setColorAt(1.0, outer)

        painter.fillRect(rect, gradient)
        painter.end()


# ===========================================================================
# Artifact/Worker Components (Moved from worker_window.py)
# ===========================================================================

from aura.resources import get_resource_path
_MERMAID_JS_PATH = get_resource_path("media/mermaid.min.js")
_MERMAID_JS: str = ""
try:
    _MERMAID_JS = _MERMAID_JS_PATH.read_text(encoding="utf-8")
except (FileNotFoundError, OSError):
    pass


def _is_previewable(language: str) -> bool:
    return language in ("html", "svg", "markdown", "mermaid")


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan."""

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

        header = QLabel("TODO LIST")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self._pulse_anims: list = []
        self.setVisible(False)

    def update_tasks(self, tasks: list[dict]) -> None:
        for anim in self._pulse_anims:
            anim.stop()
            anim.deleteLater()
        self._pulse_anims.clear()

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
            if status == "done":
                prefix, color = "✓", SUCCESS
            elif status == "active":
                prefix, color = "►", WARN
            else:
                prefix, color = "○", FG_DIM

            label = QLabel(f"{prefix} {description}")
            label.setWordWrap(True)
            font = label.font()
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setPointSize(11)
            label.setFont(font)

            if status == "active":
                font.setBold(True)
                label.setFont(font)
                effect = QGraphicsOpacityEffect(label)
                label.setGraphicsEffect(effect)
                pulse = QVariantAnimation(label)
                pulse.setStartValue(0.55); pulse.setEndValue(1.0); pulse.setDuration(900)
                pulse.setLoopCount(-1); pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
                pulse.valueChanged.connect(lambda v, e=effect: e.setOpacity(v))
                pulse.start()
                self._pulse_anims.append(pulse)

            label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            self._tasks_layout.addWidget(label)


class ArtifactCard(QFrame):
    """Interactive card with Code/Preview toggle."""

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
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        header = QWidget()
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(10, 6, 10, 6)
        
        self._header_label = QLabel(label)
        self._header_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        h_layout.addWidget(self._header_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        h_layout.addWidget(self._status_label)
        h_layout.addStretch(1)

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self._content))
        h_layout.addWidget(copy_btn)

        if _is_previewable(language):
            self._toggle_btn = QPushButton("Preview")
            self._toggle_btn.clicked.connect(self._on_toggle_view)
            h_layout.addWidget(self._toggle_btn)
        
        outer.addWidget(header)

        self._stack = QStackedWidget()
        self._code_view = QPlainTextEdit()
        self._code_view.setReadOnly(True)
        self._code_view.setFont(QFont("Geist Mono", 9))
        self._code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._code_view.setStyleSheet(f"background: {BG}; border: none; padding: 8px;")
        self._stack.addWidget(self._code_view)

        self._highlighter = PygmentsHighlighter(self._code_view.document(), language) if _language_from_path else None

        self._preview_view = QWebEngineView()
        self._stack.addWidget(self._preview_view)
        outer.addWidget(self._stack)

        self._refresh_code_view()
        self._refresh_preview()

    def _on_toggle_view(self):
        idx = 1 - self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)
        self._toggle_btn.setText("Code" if idx == 1 else "Preview")
        if idx == 1: self._refresh_preview()

    def set_target_path(self, path: str):
        self._label = path
        self._header_label.setText(self._label)
        self._language = _language_from_path(path)
        if self._highlighter: self._highlighter.deleteLater()
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
        if not active: self._flush_typing()

    def _start_typing(self, target: str):
        if not self._typing_timer:
            self._typing_timer = QTimer(self)
            self._typing_timer.timeout.connect(self._on_typing_tick)
        self._typing_target = target
        if self._typing_position > len(target):
            self._typing_position = 0
        if not self._typing_timer.isActive(): self._typing_timer.start(33)

    def _on_typing_tick(self):
        if self._typing_position >= len(self._typing_target):
            self._typing_timer.stop(); return
        self._typing_position += 5
        self._code_view.setPlainText(self._typing_target[:self._typing_position])
        self._auto_size()

    def _flush_typing(self):
        if self._typing_timer: self._typing_timer.stop()
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

    def _refresh_preview(self):
        if self._language == "html": self._preview_view.setHtml(self._content)
        elif self._language == "mermaid":
            mermaid_include = f"<script>{_MERMAID_JS}</script>" if _MERMAID_JS else ""
            html = f"<html><body>{mermaid_include}<div class='mermaid'>{self._content}</div><script>mermaid.initialize({{startOnLoad:true}})</script></body></html>"
            self._preview_view.setHtml(html)


class WorkerLogCard(QFrame):
    """Card for typewriter worker activity log."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workerLogCard")
        self.setStyleSheet(f"QFrame#workerLogCard {{ background: rgba(28, 28, 34, 0.4); border: 1px solid {BORDER}; border-radius: 8px; }}")
        layout = QVBoxLayout(self)
        self._header = QLabel("⚡ Worker Activity")
        self._header.setStyleSheet(f"color: {ACCENT}; font-weight: 700;")
        layout.addWidget(self._header)
        self._content_view = QPlainTextEdit()
        self._content_view.setReadOnly(True)
        self._content_view.setFont(QFont("Geist Mono", 10))
        self._content_view.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._content_view)
        self._full, self._visible, self._timer = "", "", QTimer(self)
        self._timer.timeout.connect(self._on_tick); self._timer.setInterval(20)

    def append_text(self, text: str, is_reasoning=False):
        self._full += text
        if not self._timer.isActive(): self._timer.start()

    def _on_tick(self):
        if len(self._visible) >= len(self._full): self._timer.stop(); return
        self._visible += self._full[len(self._visible):len(self._visible)+2]
        self._content_view.setPlainText(self._visible)
        h = self._content_view.document().size().height() + 15
        self._content_view.setFixedHeight(int(max(120, min(h, 600))))

    def clear(self):
        self._timer.stop(); self._full = ""; self._visible = ""; self._content_view.setPlainText("")


class AuraPlayground(QWidget):
    """Right-side panel for worker output."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Playground"))
        self._todo_widget = TodoListWidget()
        layout.addWidget(self._todo_widget)
        
        self._scroll = QScrollArea()
        self._container = QWidget()
        self._card_layout = QVBoxLayout(self._container)
        self._card_layout.addStretch(1)
        self._scroll.setWidget(self._container); self._scroll.setWidgetResizable(True)
        layout.addWidget(self._scroll, 1)

        self._artifacts, self._controllers, self._auras, self._terminal_cards, self._log_card = {}, {}, {}, {}, None
        
        # Follow active worker output unless the user scrolls away from the bottom.
        self._auto_follow_bottom = True
        self._last_scroll_max = 0
        self._scroll.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

    def _on_scroll_range_changed(self, min_val: int, max_val: int) -> None:
        """If we were at the bottom before the range increased, stay at the bottom."""
        if max_val > self._last_scroll_max and self._auto_follow_bottom:
            self._set_scrollbar_to_bottom()
        self._last_scroll_max = max_val

    def _on_scroll_value_changed(self, value: int) -> None:
        bar = self._scroll.verticalScrollBar()
        self._auto_follow_bottom = bar.maximum() - value <= 60

    def _set_scrollbar_to_bottom(self):
        self._scroll.verticalScrollBar().setValue(self._scroll.verticalScrollBar().maximum())

    def _scroll_to_bottom(self, force: bool = False):
        """Keep the newest worker output visible when following the stream."""
        if not force and not self._auto_follow_bottom:
            return
        self._auto_follow_bottom = True
        self._set_scrollbar_to_bottom()
        if force:
            for delay in (0, 50, 150):
                QTimer.singleShot(delay, self._set_scrollbar_to_bottom)

    def begin_assistant(self):
        for w in list(self._auras.values()): w.deleteLater()
        for w in list(self._terminal_cards.values()): w.deleteLater()
        self._artifacts.clear(); self._auras.clear(); self._controllers.clear(); self._terminal_cards.clear()
        if self._log_card: self._log_card.clear(); self._log_card.setVisible(False)
        self._last_scroll_max = 0
        self._scroll_to_bottom(force=True)

    def _ensure_log_card(self):
        if not self._log_card:
            self._log_card = WorkerLogCard(self)
            self._card_layout.insertWidget(self._card_layout.count()-1, self._log_card)
        self._log_card.setVisible(True)
        return self._log_card

    def append_reasoning(self, text: str): 
        self._ensure_log_card().append_text(text)
        self._scroll_to_bottom()

    def append_content(self, text: str): 
        self._ensure_log_card().append_text(text)
        self._scroll_to_bottom()

    def add_tool_call(self, worker_tool_id: str, name: str):
        from aura.gui.cards import TerminalCard
        c = ToolStreamController(name, self)
        self._controllers[worker_tool_id] = c
        if name == "update_todo_list": c.todo_updated.connect(self.update_todo_list)
        if name in ("write_file", "edit_file"):
            aid = f"file-{worker_tool_id}"
            card = ArtifactCard(aid, "Targeting...", "text", "", self)
            self._artifacts[aid] = card
            aura = AuraWidget(card, parent=self)
            self._auras[aid] = aura
            self._card_layout.insertWidget(self._card_layout.count()-1, aura)
            c.path_resolved.connect(card.set_target_path)
            c.content_updated.connect(card.update_content)
            card.set_streaming(True); aura.start_aura()
        
        if name == "run_terminal_command":
            # Create the terminal card immediately with a placeholder.
            # MainWindow will route output here via append_terminal_output.
            card = TerminalCard(command="...")
            self._terminal_cards[worker_tool_id] = card
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)
            c.command_resolved.connect(card.set_command)
        
        self._scroll_to_bottom()

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        controller = self._controllers.get(worker_tool_id)
        if controller is None:
            return
        controller.append_fragment(fragment)
        self._scroll_to_bottom()

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str):
        if worker_tool_id in self._controllers: self._controllers.pop(worker_tool_id).finalize(ok, result)
        aid = f"file-{worker_tool_id}"
        if aid in self._artifacts:
            self._artifacts[aid].set_streaming(False)
            self._auras[aid].stop_aura()
        
        if worker_tool_id in self._terminal_cards:
            # For terminal results, we want to set the final state based on exit_code
            exit_code = 0
            try:
                import json
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    exit_code = parsed.get("exit_code", 0)
            except Exception:
                pass
            self._terminal_cards[worker_tool_id].set_result(exit_code)
        
        self._scroll_to_bottom()

    def update_todo_list(self, tasks: list): self._todo_widget.update_tasks(tasks)

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        from aura.gui.cards import DiffCard
        card = DiffCard(rel_path, old, new, decision, is_new_file)
        # Use AuraWidget for visual consistency with streaming cards
        wrapper = AuraWidget(card, parent=self)
        self._card_layout.insertWidget(self._card_layout.count() - 1, wrapper)
        # Scroll to bottom
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    def add_error(self, message: str) -> None:
        from aura.gui.cards import ErrorCard
        card = ErrorCard("Worker Error", message)
        self._card_layout.insertWidget(self._card_layout.count() - 1, card)
        self._scroll_to_bottom()

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        from aura.gui.cards import TerminalCard
        # Find existing terminal card for this tool call or create new
        if worker_tool_id not in self._terminal_cards:
            card = TerminalCard(command="...")
            self._terminal_cards[worker_tool_id] = card
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)
        
        self._terminal_cards[worker_tool_id].append_output(text)
        self._scroll_to_bottom()

    def worker_finished(self, ok: bool, s: str): pass
    def worker_cancelled(self): pass
    def clear(self): self.begin_assistant()
    def add_mermaid_artifact(self, code: str): pass
