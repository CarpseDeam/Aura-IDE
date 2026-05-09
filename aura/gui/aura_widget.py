"""Shared UI components for the Aura glass theme."""
from __future__ import annotations

import math
from pathlib import Path

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
    QObject
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
        
        if label:
            self._label = QLabel(label)
            self._label.setStyleSheet(f"color: {FG}; font-size: 10px; font-weight: 600;")
            self._label.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)

    def _get_track_style(self) -> str:
        bg = ACCENT if self._checked else BG_RAISED
        border = ACCENT if self._checked else BORDER
        return f"background: {bg}; border: 1px solid {border}; border-radius: 8px;"

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def setChecked(self, checked: bool):
        self._checked = checked
        self._track.setStyleSheet(self._get_track_style())
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
    """AuraWidget — soft breathing glow effect for active-streaming indication."""

    def __init__(
        self,
        child: QWidget,
        glow_color: str = "#7aa2f7",
        glow_spread: float = 20.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glow_color = QColor(glow_color)
        self._glow_spread = glow_spread
        self._active = False
        self._opacity = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(glow_spread), int(glow_spread), int(glow_spread), int(glow_spread))
        layout.setSpacing(0)
        layout.addWidget(child)

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.2)
        self._anim.setEndValue(0.7)
        self._anim.setDuration(1200)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.valueChanged.connect(self._on_anim_val)

        # Optimization: only paint if visible/active
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def start_aura(self) -> None:
        self._active = True
        self._anim.start()
        self.update()

    def stop_aura(self) -> None:
        self._active = False
        self._anim.stop()
        self._opacity = 0.0
        self.update()

    def set_glow_state(self, state: str) -> None:
        """Update the glow color based on the current worker/planner state."""
        if state == "thinking":
            self._glow_color = QColor("#bb9af7")  # Purple
        elif state == "coding":
            self._glow_color = QColor("#7dcfff")  # Cyan
        elif state == "error":
            self._glow_color = QColor("#f7768e")  # Red
        else:
            self._glow_color = QColor("#7aa2f7")  # Default blue
        self.update()

    def _on_anim_val(self, val: float) -> None:
        self._opacity = val
        self.update()

    def paintEvent(self, event) -> None:
        if not self._active:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        inner_rect = self.layout().itemAt(0).widget().geometry()

        # Simple radial gradient at the center of the widget
        center = rect.center()
        radius = max(rect.width(), rect.height()) / 1.5

        gradient = QRadialGradient(center, radius)
        
        c = self._glow_color
        inner = QColor(c.red(), c.green(), c.blue(), int(255 * self._opacity))
        mid = QColor(c.red(), c.green(), c.blue(), int(100 * self._opacity))
        outer = QColor(c.red(), c.green(), c.blue(), 0)

        inner_pos = (min(inner_rect.width(), inner_rect.height()) / 2.0) / radius
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
        self._code_view.setStyleSheet(f"background: {BG}; border: none; padding: 8px;")
        self._stack.addWidget(self._code_view)

        self._highlighter = PygmentsHighlighter(self._code_view.document(), language) if _language_from_path else None

        self._preview_view = QWebEngineView()
        self._stack.addWidget(self._preview_view)
        outer.addWidget(self._stack, 1)

        self._refresh_code_view()
        self._refresh_preview()

    def _on_toggle_view(self):
        idx = 1 - self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)
        self._toggle_btn.setText("Code" if idx == 1 else "Preview")
        if idx == 1: self._refresh_preview()

    def set_target_path(self, path: str):
        self._label = Path(path).name
        self._header_label.setText(self._label)
        self._language = _language_from_path(path)
        if self._highlighter: self._highlighter.deleteLater()
        self._highlighter = PygmentsHighlighter(self._code_view.document(), self._language)

    def update_content(self, content: str):
        self._content = content
        if self._streaming: self._start_typing(content)
        else: self._refresh_code_view()
        self._refresh_preview()

    def set_streaming(self, active: bool):
        self._streaming = active
        self._status_label.setText("● streaming" if active else "✓ done")
        if not active: self._flush_typing()

    def _start_typing(self, target: str):
        if not self._typing_timer:
            self._typing_timer = QTimer(self)
            self._typing_timer.timeout.connect(self._on_typing_tick)
        self._typing_target = target
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
        self._code_view.setFixedHeight(int(max(120, min(h, 600))))

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

        self._artifacts, self._controllers, self._auras, self._log_card = {}, {}, {}, None

    def begin_assistant(self):
        for w in list(self._auras.values()): w.deleteLater()
        self._artifacts.clear(); self._auras.clear(); self._controllers.clear()
        if self._log_card: self._log_card.clear(); self._log_card.setVisible(False)

    def _ensure_log_card(self):
        if not self._log_card:
            self._log_card = WorkerLogCard(self)
            self._card_layout.insertWidget(self._card_layout.count()-1, self._log_card)
        self._log_card.setVisible(True)
        return self._log_card

    def append_reasoning(self, text: str): self._ensure_log_card().append_text(text)
    def append_content(self, text: str): self._ensure_log_card().append_text(text)

    def add_tool_call(self, worker_tool_id: str, name: str):
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

    def append_tool_args(self, worker_tool_id: str, fragment: str):
        if worker_tool_id in self._controllers: self._controllers[worker_tool_id].append_fragment(fragment)

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str):
        if worker_tool_id in self._controllers: self._controllers.pop(worker_tool_id).finalize(ok, result)
        aid = f"file-{worker_tool_id}"
        if aid in self._artifacts:
            self._artifacts[aid].set_streaming(False)
            self._auras[aid].stop_aura()

    def update_todo_list(self, tasks: list): self._todo_widget.update_tasks(tasks)
    def worker_finished(self, ok: bool, s: str): pass
    def worker_cancelled(self): pass
    def clear(self): self.begin_assistant()
    def add_mermaid_artifact(self, code: str): pass
