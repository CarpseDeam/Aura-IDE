"""Assistant message card with reasoning, content, and inline tool cards."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import media_path
from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.cards._helpers import _CODE_FENCE_RE, _fade_in_widget, _MarkdownTextBlock
from aura.gui.cards._stream_label import _StreamLabel
from aura.gui.cards.code_block_card import CodeBlockCard
from aura.gui.cards.tool_call_card import ToolCallCard
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BG_RAISED, FG, FG_ITALIC, SUCCESS_DIM, WARN
from aura.gui.worker_log_stream.formatter import normalize_assistant_display_text

if TYPE_CHECKING:
    from aura.gui.chat_view import ChatView


class AssistantCard(QFrame):
    def __init__(self, compact_tools: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("assistantCard")
        self._compact_tools = compact_tools
        self._compact_tool_active: int = 0
        self._compact_tool_names: list[str] = []
        self._chat_view: ChatView | None = None
        self._assistant_text: str = ""

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(16, 14, 16, 14)
        self._outer.setSpacing(6)

        # Header row: "Aura" on left, tool status on right.
        header_row = QWidget(self)
        header_row.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        header = QLabel("Aura", parent=header_row)
        header.setObjectName("assistantHeader")
        header_layout.addWidget(header)

        # Copy button for full assistant message text
        self._copy_btn = QToolButton(header_row)
        self._copy_btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        self._copy_btn.setIconSize(QSize(16, 16))
        self._copy_btn.setToolTip("Copy message")
        self._copy_btn.setStyleSheet(
            f"QToolButton {{ border: none; border-radius: 3px; padding: 2px; }} "
            f"QToolButton:hover {{ background: {BG_RAISED}; }}"
        )
        self._copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(self._copy_btn)

        header_layout.addStretch(1)

        self._thinking_label = QLabel("", parent=header_row)
        self._thinking_label.setObjectName("thinkingIndicator")
        self._thinking_label.setVisible(False)
        f = self._thinking_label.font()
        f.setPointSize(10)
        self._thinking_label.setFont(f)
        self._thinking_label.setStyleSheet(f"color: {WARN}; font-style: italic;")
        header_layout.addWidget(self._thinking_label)

        # Animated dots for the thinking indicator
        self._thinking_anim: QVariantAnimation | None = None
        self._thinking_dots: int = 0

        self._tool_status = QLabel("", parent=header_row)
        self._tool_status.setObjectName("toolStatus")
        font = self._tool_status.font()
        font.setPointSize(10)
        self._tool_status.setFont(font)
        self._tool_status.setTextFormat(Qt.TextFormat.RichText)
        self._tool_status.setVisible(False)
        header_layout.addWidget(self._tool_status)

        self._outer.addWidget(header_row)

        # Reasoning: lazy — created on first reasoning delta.
        self._reasoning_section: _CollapsibleSection | None = None
        self._reasoning_label: _StreamLabel | None = None
        self._reasoning_scroll_area: QScrollArea | None = None
        self._reasoning_scroll_timer: QTimer | None = None

        # Content: the streamed answer.
        self._content_label = _StreamLabel(italic=False, parent=self)
        self._content_label.setVisible(False)
        self._outer.addWidget(self._content_label)

        # Tool calls grouped under the assistant turn — indented frame with a
        # left rule so the cluster reads as supporting info under the message.
        self._tool_cluster = QFrame(self)
        self._tool_cluster.setObjectName("toolCluster")
        self._tool_cluster_layout = QVBoxLayout(self._tool_cluster)
        self._tool_cluster_layout.setContentsMargins(16, 6, 0, 0)
        self._tool_cluster_layout.setSpacing(5)
        self._tool_cluster.setVisible(False)
        self._outer.addWidget(self._tool_cluster)

        self._tool_cards: dict[str, ToolCallCard] = {}
        self._reasoning_finalized = False

        # Footer: diff cards / usage / errors injected later.
        self._footer = QVBoxLayout()
        self._footer.setContentsMargins(0, 4, 0, 0)
        self._footer.setSpacing(6)
        self._outer.addLayout(self._footer)

    # ---- streaming hooks --------------------------------------------------

    def append_reasoning(self, text: str) -> None:
        self._start_thinking_animation()
        if self._reasoning_label is None:
            self._reasoning_label = _StreamLabel(italic=True, parent=self)
            scroll_area = QScrollArea(self)
            scroll_area.setWidgetResizable(True)
            scroll_area.setWidget(self._reasoning_label)
            scroll_area.setMaximumHeight(500)
            scroll_area.setMinimumHeight(190)
            scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            self._reasoning_scroll_area = scroll_area
            section = _CollapsibleSection(
                "Thinking…", scroll_area, start_open=False, prominent=True
            )
            self._reasoning_section = section
            # Insert reasoning at the top, after header (index 1).
            self._outer.insertWidget(1, section)
        self._reasoning_label.append(text)
        # Auto-scroll the reasoning box to the bottom (coalesced via timer)
        if self._reasoning_scroll_area is not None:
            if self._reasoning_scroll_timer is None:
                self._reasoning_scroll_timer = QTimer(self)
                self._reasoning_scroll_timer.setSingleShot(True)
                self._reasoning_scroll_timer.timeout.connect(self._scroll_reasoning_to_bottom)
            if not self._reasoning_scroll_timer.isActive():
                self._reasoning_scroll_timer.start(50)

    def _scroll_reasoning_to_bottom(self) -> None:
        if self._reasoning_scroll_area is not None:
            sb = self._reasoning_scroll_area.verticalScrollBar()
            sb.setValue(sb.maximum())

    def reasoning_done(self) -> None:
        if self._reasoning_finalized:
            return
        self._reasoning_finalized = True
        if self._reasoning_section is not None:
            self._reasoning_section.set_title("Thinking")
            # Keep reasoning open so the user can review it
        self.finalize_reasoning()

    def append_content(self, text: str) -> None:
        self._stop_thinking_animation()
        if not self._content_label.isVisible():
            self._content_label.setVisible(True)
            # Keep reasoning visible so user can see the thinking
        self._assistant_text += text
        self._content_label.append(text)

    def finalize_reasoning(self) -> None:
        """Replace the streaming reasoning label with a rich layout."""
        if self._reasoning_label is None or self._reasoning_scroll_area is None:
            return

        text = self._reasoning_label.text_buffer()
        if not text:
            return

        # If no fenced code blocks, just render markdown in the existing label
        if not _CODE_FENCE_RE.search(text):
            self._reasoning_label.stop_timer()
            html = _render_markdown_with_code(text, color=FG_ITALIC, italic=True)
            self._reasoning_label.setTextFormat(Qt.TextFormat.RichText)
            self._reasoning_label.setText(html)
            return

        # Otherwise, build a rich container
        container = self._build_rich_container(text, color=FG_ITALIC, italic=True)
        self._reasoning_label.stop_timer()
        self._reasoning_scroll_area.setWidget(container)
        self._reasoning_label = None

    # ---- compact tool status --------------------------------------------


    def notify_compact_tool_start(self, name: str) -> None:
        self._compact_tool_active += 1
        self._tool_status.setVisible(True)
        self._tool_status.setText(
            f"<span style='color:{WARN};'>📄 Reading files…</span>"
        )

    def notify_compact_tool_done(self, name: str) -> None:
        self._compact_tool_active = max(0, self._compact_tool_active - 1)
        self._compact_tool_names.append(name)
        if self._compact_tool_active == 0:
            n = len(self._compact_tool_names)
            self._tool_status.setText(
                f"<span style='color:{SUCCESS_DIM};'>✓ {n} tool{'s' if n != 1 else ''}</span>"
            )

    def reset_compact_tool_state(self) -> None:
        self._compact_tool_active = 0
        self._compact_tool_names.clear()
        self._tool_status.setVisible(False)
        self._stop_thinking_animation()

    # ---- simple public API for Workshop use ------------------------------

    def show_thinking_message(self, text: str = "Thinking") -> None:
        """Show a thinking indicator with the given message."""
        self._thinking_label.setText(text)
        self._start_thinking_animation()

    def set_content(self, text: str) -> None:
        """Set the assistant response content (non-streaming)."""
        self._stop_thinking_animation()
        text = normalize_assistant_display_text(text)
        self._content_label.stop_timer()
        self._content_label.reset_buffer()
        self._content_label.append(text)
        self._content_label._flush()
        self._content_label.setVisible(True)
        self._assistant_text = text
        self.finalize_content()

    def set_error(self, text: str) -> None:
        """Display an error message on the card."""
        self._stop_thinking_animation()
        self._assistant_text = text
        self._content_label.stop_timer()
        self._content_label.reset_buffer()
        self._content_label.append(text)
        self._content_label._flush()
        self._content_label.setVisible(True)
        # Render error as plain-ish markdown (no code-block reflow needed)
        html = _render_markdown_with_code(text)
        self._content_label.setTextFormat(Qt.TextFormat.RichText)
        self._content_label.setText(html)

    # ---- copy button -----------------------------------------------------

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._assistant_text)
        self._copy_btn.setIcon(QIcon(str(media_path("check.svg"))))
        self._copy_btn.setText("")
        self._copy_btn.setToolTip("Copied!")
        QTimer.singleShot(2000, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        self._copy_btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        self._copy_btn.setText("")
        self._copy_btn.setToolTip("Copy message")

    # ---- thinking animation ---------------------------------------------

    def _start_thinking_animation(self) -> None:
        if self._thinking_anim is not None:
            return
        self._thinking_label.setVisible(True)
        self._thinking_anim = QVariantAnimation(self)
        self._thinking_anim.setStartValue(0)
        self._thinking_anim.setEndValue(3)
        self._thinking_anim.setDuration(1200)
        self._thinking_anim.setLoopCount(-1)
        self._thinking_anim.valueChanged.connect(self._on_thinking_tick)
        self._thinking_anim.start()

    def _stop_thinking_animation(self) -> None:
        if self._thinking_anim is not None:
            self._thinking_anim.stop()
            self._thinking_anim.deleteLater()
            self._thinking_anim = None
        self._thinking_label.setVisible(False)
        self._thinking_label.setText("")

    def _on_thinking_tick(self, value: int) -> None:
        dots = "." * (value + 1)
        self._thinking_label.setText(f"Thinking{dots}")

    # ---- tool cards -----------------------------------------------------

    def add_tool_card(self, tool_call_id: str, name: str) -> "ToolCallCard | None":
        if self._compact_tools:
            self.notify_compact_tool_start(name)
            return None
        card = ToolCallCard(name, parent=self)
        self._tool_cards[tool_call_id] = card
        if not self._tool_cluster.isVisible():
            self._tool_cluster.setVisible(True)
        self._tool_cluster_layout.addWidget(card)
        if self._chat_view is None or not self._chat_view._is_bulk_updating:
            _fade_in_widget(card)
        return card

    def get_tool_card(self, tool_call_id: str) -> "ToolCallCard | None":
        if self._compact_tools:
            return None
        return self._tool_cards.get(tool_call_id)

    def add_footer_widget(self, w: QWidget) -> None:
        self._footer.addWidget(w)
        if self._chat_view is None or not self._chat_view._is_bulk_updating:
            _fade_in_widget(w)

    def finalize_content(self) -> None:
        """Replace the streaming label with a rich layout that renders code
        blocks as CodeBlockCard widgets instead of inline HTML pre blocks.
        
        Supports multiple turns by appending a new rich container each time
        the stream finishes a round.
        """
        self._stop_thinking_animation()
        text = self._content_label.text_buffer()
        if not text:
            return
        # Normalize display text to add paragraph breaks between beats.
        text = normalize_assistant_display_text(text)

        # If no fenced code blocks, fall back to the old inline HTML render
        if not _CODE_FENCE_RE.search(text):
            self._content_label.stop_timer()
            html = _render_markdown_with_code(text)
            self._content_label.setTextFormat(Qt.TextFormat.RichText)
            self._content_label.setText(html)
            return

        # Build a container widget to replace the streaming label
        container = self._build_rich_container(text)

        # Swap the streaming label out for the rich container
        idx = self._outer.indexOf(self._content_label)
        if idx >= 0:
            self._content_label.stop_timer()
            self._content_label.hide()
            # Insert new container at the same position
            self._outer.insertWidget(idx, container)
            # We keep the label in the layout so subsequent turns can re-show it 
            # and append to it. We just move it to the end of the current content.
            self._outer.removeWidget(self._content_label)
            self._outer.insertWidget(idx + 1, self._content_label)
            # Reset the label buffer so the next turn starts fresh
            self._content_label.reset_buffer()

    def _build_rich_container(
        self, text: str, color: str | None = None, italic: bool = False
    ) -> QWidget:
        """Build a container widget with interleaved text (markdown) and code cards."""
        segments = self._parse_content(text)
        container = QWidget(self)
        container.setStyleSheet("background: transparent;")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(8)

        for seg_type, content, lang in segments:
            if seg_type == "text":
                if content.strip():
                    html = _render_markdown_with_code(content, color=color, italic=italic)
                    block = _MarkdownTextBlock(html, parent=container)
                    # Use the provided color or the theme FG.
                    c = color if color else FG
                    block.setStyleSheet(f"background: transparent; border: none; color: {c};")
                    container_layout.addWidget(block)
            elif seg_type == "code":
                card = CodeBlockCard(lang, content, parent=container)
                container_layout.addWidget(card)
                if lang == "mermaid" and self._chat_view is not None:
                    self._chat_view.mermaid_detected.emit(content)
        return container


    def _parse_content(self, text: str) -> list[tuple[str, str, str]]:
        """Split text into a list of segments around fenced code blocks.
        Each segment is (type, content, language).
        type is 'text' or 'code'.
        """
        segments: list[tuple[str, str, str]] = []
        last_end = 0
        for m in _CODE_FENCE_RE.finditer(text):
            before = text[last_end:m.start()]
            if before.strip():
                segments.append(("text", before, ""))
            lang = (m.group(1) or "").strip().lower()
            code = m.group(2)
            segments.append(("code", code, lang))
            last_end = m.end()
        after = text[last_end:]
        if after.strip():
            segments.append(("text", after, ""))
        if not segments:
            segments.append(("text", text, ""))
        return segments
