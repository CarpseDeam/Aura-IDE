"""Chat transcript: scrollable column of message cards.

Card types:
- UserCard: text + optional image thumbnails
- AssistantCard: collapsible reasoning section, content, tool/diff cards inline
- ToolCallCard: header + collapsible args + result (running/done/failed)
- DiffCard: same as approval modal but read-only, shown after apply/reject
- ErrorCard: red, surfaces ApiError/tool errors verbatim
"""
from __future__ import annotations

import base64
import html as _html
import json
import re

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QFont, QPixmap, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.diff_dialog import render_unified_diff
from aura.gui.aura_widget import AuraWidget
from aura.gui.theme import (
    ACCENT,
    BG,
    BG_ALT,
    BORDER,
    DANGER,
    DIFF_ADD_BG,
    DIFF_DEL_BG,
    FG,
    FG_BODY_USER,
    FG_DIM,
    FG_ITALIC,
    FG_MUTED,
    SUCCESS,
    SUCCESS_DIM,
    TERMINAL_BG,
    WARN,
)

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.util import ClassNotFound
    _HAVE_PYGMENTS = True
except ImportError:  # pragma: no cover — declared in pyproject, but soft-fail.
    _HAVE_PYGMENTS = False


_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-.]*)\n(.*?)(?:```|\Z)", re.DOTALL)


def _render_code_block(lang: str, code: str) -> str:
    """Pygments HTML for one code block, with inline styles (no class= required)."""
    if not _HAVE_PYGMENTS:
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<pre style="background: transparent; color:{FG}; '
            f'border: none; border-radius:6px; padding:8px; '
            f'font-family:\'Geist Mono\',\'JetBrains Mono\',monospace;\">{escaped}</pre>'
        )
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    formatter = HtmlFormatter(
        style="dracula",
        noclasses=True,
        nowrap=False,
        prestyles=(
            "background: transparent; border: none; border-radius:6px; "
            "padding:8px; font-family:'Geist Mono','JetBrains Mono',monospace; "
            "font-size:12px; white-space:pre;"
        ),
    )
    return highlight(code, lexer, formatter)


def _render_markdown_with_code(text: str) -> str:
    """Render a markdown string to Qt-friendly HTML, swapping fenced code
    blocks for Pygments-highlighted HTML. Inline code (single backticks) is
    left to the markdown renderer.
    """
    if not text:
        return ""

    blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip().lower()
        code = match.group(2)
        idx = len(blocks)
        blocks.append(_render_code_block(lang, code))
        # Use a placeholder that won't be mangled by markdown rendering.
        return f"\n\nAURACODEPLACEHOLDER{idx}ENDAURA\n\n"

    intermediate = _CODE_FENCE_RE.sub(stash, text)

    doc = QTextDocument()
    doc.setMarkdown(intermediate)
    html = doc.toHtml()

    # QTextDocument.toHtml() bakes color rules into every <p style="…"> element,
    # which then override our QSS body color and make the text look gray on dark.
    # Strip those colors so our wrapper div takes effect — pygments blocks are
    # still placeholders at this point and aren't affected.
    html = re.sub(r"color\s*:\s*#[0-9a-fA-F]+\s*;?", "", html)

    for i, block in enumerate(blocks):
        token = f"AURACODEPLACEHOLDER{i}ENDAURA"
        # Markdown wraps the bare token in paragraph tags — strip them.
        wrapped = re.compile(r"<p[^>]*>\s*" + re.escape(token) + r"\s*</p>")
        if wrapped.search(html):
            html = wrapped.sub(block, html, count=1)
        else:
            html = html.replace(token, block, 1)
    # Final wrap: enforce body color + a comfortable line-height for paragraphs.
    return f'<div style="color: {FG}; line-height: 145%;">{html}</div>'


def _wrap_body_text(text: str, color: str) -> str:
    """Escape plain text and wrap it in a div with explicit color and a comfortable
    line-height — QLabel ignores QSS line-height, so rich-text wrapping is the only way.
    """
    escaped = _html.escape(text).replace("\n", "<br/>")
    return f'<div style="color: {color}; line-height: 145%;">{escaped}</div>'


def _mono_font(pt: int = 10) -> QFont:
    f = QFont("Geist Mono, JetBrains Mono, Consolas, Menlo, monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    f.setPointSize(pt)
    return f


def _fade_in_widget(widget: QWidget, duration: int = 150) -> None:
    """Apply a fade-in opacity animation to a newly-added widget."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    effect.setOpacity(0.0)

    anim = QPropertyAnimation(effect, b"opacity")
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # Clean up the effect after animation completes so it doesn't interfere
    # with sub-widget rendering (QPlainTextEdit etc.)
    def _cleanup():
        try:
            if widget is not None:
                widget.setGraphicsEffect(None)
            effect.deleteLater()
            anim.deleteLater()
        except RuntimeError:
            pass  # C++ object already deleted (widget/effect cleaned up by parent deletion)
    anim.finished.connect(_cleanup)
    anim.start()


class _CollapsibleSection(QFrame):
    """A toggle button + body widget that collapses on click."""

    OPEN_CARET = "▾"   # ▾
    CLOSED_CARET = "▸"  # ▸

    def __init__(
        self,
        title: str,
        body: QWidget,
        start_open: bool = False,
        prominent: bool = False,
    ) -> None:
        super().__init__()
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._toggle = QToolButton()
        self._toggle.setObjectName("reasoningToggle" if prominent else "sectionToggle")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        self._title = title
        self._body = body
        self._open = start_open
        body.setVisible(start_open)
        layout.addWidget(self._toggle)
        layout.addWidget(body)
        self._refresh_text()

    def _refresh_text(self) -> None:
        caret = self.OPEN_CARET if self._open else self.CLOSED_CARET
        self._toggle.setText(f"{caret}  {self._title}")

    def _on_toggle(self) -> None:
        self._open = not self._open
        self._body.setVisible(self._open)
        self._refresh_text()

    def set_title(self, title: str) -> None:
        self._title = title
        self._refresh_text()

    def set_open(self, value: bool) -> None:
        self._open = value
        self._body.setVisible(value)
        self._refresh_text()


# -----------------------------------------------------------------------------
# Cards
# -----------------------------------------------------------------------------


class UserCard(QFrame):
    def __init__(self, text: str, image_b64s: list[str] | None = None) -> None:
        super().__init__()
        self.setObjectName("userCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        header = QLabel("You")
        header.setObjectName("userHeader")
        layout.addWidget(header)

        if image_b64s:
            row = QHBoxLayout()
            row.setSpacing(8)
            for b64 in image_b64s:
                thumb = self._make_thumb(b64)
                row.addWidget(thumb)
            row.addStretch(1)
            layout.addLayout(row)

        if text:
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setText(_wrap_body_text(text, FG_BODY_USER))
            layout.addWidget(body)

    def _make_thumb(self, b64: str) -> QLabel:
        try:
            data = base64.b64decode(b64)
            pix = QPixmap()
            pix.loadFromData(data)
            scaled = pix.scaled(
                160, 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label = QLabel()
            label.setPixmap(scaled)
            label.setStyleSheet(f"border: 1px solid {BORDER}; border-radius: 4px;")
            return label
        except Exception as exc:
            label = QLabel(f"[image: {exc}]")
            label.setStyleSheet(f"color: {DANGER};")
            return label


class _StreamLabel(QLabel):
    """Word-wrapping label that grows as text is appended. Tokens accumulate in a
    buffer and the UI is flushed at most 30 fps to keep the GUI thread responsive
    on fast token streams."""

    def __init__(self, italic: bool = False) -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._italic = italic
        if italic:
            self.setObjectName("reasoning")
            self.setStyleSheet(f"color: {FG_ITALIC}; font-style: italic;")
        else:
            self.setStyleSheet(f"color: {FG};")
        # Use rich text so we can control line-height during streaming.
        self.setTextFormat(Qt.TextFormat.RichText)
        self._buf = ""
        self._dirty = False

        # Throttle: update UI at most 30fps (33ms interval)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.start()

    def append(self, text: str) -> None:
        self._buf += text
        self._dirty = True
        # Don't call setText here — let the timer flush it

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self._italic:
            escaped = _html.escape(self._buf).replace("\n", "<br/>")
            self.setText(
                f'<div style="color: {FG_ITALIC}; line-height: 145%; font-style: italic;">'
                f"{escaped}</div>"
            )
        else:
            self.setText(_wrap_body_text(self._buf, FG))

    def stop_timer(self) -> None:
        self._timer.stop()

    def text_buffer(self) -> str:
        return self._buf


class AssistantCard(QFrame):
    def __init__(self, compact_tools: bool = False) -> None:
        super().__init__()
        self.setObjectName("assistantCard")
        self._compact_tools = compact_tools
        self._compact_tool_active: int = 0
        self._compact_tool_names: list[str] = []
        self._chat_view: ChatView | None = None

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(16, 14, 16, 14)
        self._outer.setSpacing(6)

        # Header row: "Aura" on left, tool status on right.
        header_row = QWidget()
        header_row.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        header = QLabel("Aura")
        header.setObjectName("assistantHeader")
        header_layout.addWidget(header)

        header_layout.addStretch(1)

        self._thinking_label = QLabel("")
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

        self._tool_status = QLabel("")
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

        # Content: the streamed answer.
        self._content_label = _StreamLabel(italic=False)
        self._content_label.setVisible(False)
        self._outer.addWidget(self._content_label)

        # Tool calls grouped under the assistant turn — indented frame with a
        # left rule so the cluster reads as supporting info under the message.
        self._tool_cluster = QFrame()
        self._tool_cluster.setObjectName("toolCluster")
        self._tool_cluster_layout = QVBoxLayout(self._tool_cluster)
        self._tool_cluster_layout.setContentsMargins(16, 6, 0, 0)
        self._tool_cluster_layout.setSpacing(5)
        self._tool_cluster.setVisible(False)
        self._outer.addWidget(self._tool_cluster)

        self._tool_cards: dict[str, ToolCallCard] = {}

        # Footer: diff cards / usage / errors injected later.
        self._footer = QVBoxLayout()
        self._footer.setContentsMargins(0, 4, 0, 0)
        self._footer.setSpacing(6)
        self._outer.addLayout(self._footer)

    # ---- streaming hooks --------------------------------------------------

    def append_reasoning(self, text: str) -> None:
        self._start_thinking_animation()
        if self._reasoning_label is None:
            self._reasoning_label = _StreamLabel(italic=True)
            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setWidget(self._reasoning_label)
            scroll_area.setMaximumHeight(500)
            scroll_area.setMinimumHeight(190)
            scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            self._reasoning_scroll_area = scroll_area
            section = _CollapsibleSection(
                "Thinking…", scroll_area, start_open=True, prominent=True
            )
            self._reasoning_section = section
            # Insert reasoning at the top, after header (index 1).
            self._outer.insertWidget(1, section)
        self._reasoning_label.append(text)
        # Auto-scroll the reasoning box to the bottom
        if self._reasoning_scroll_area is not None:
            sb = self._reasoning_scroll_area.verticalScrollBar()
            sb.setValue(sb.maximum())

    def reasoning_done(self) -> None:
        if self._reasoning_section is not None:
            self._reasoning_section.set_title("Thinking")
            # Keep reasoning open so the user can review it

    def append_content(self, text: str) -> None:
        self._stop_thinking_animation()
        if not self._content_label.isVisible():
            self._content_label.setVisible(True)
            # Keep reasoning visible so user can see the thinking
        self._content_label.append(text)

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
        card = ToolCallCard(name)
        self._tool_cards[tool_call_id] = card
        if not self._tool_cluster.isVisible():
            self._tool_cluster.setVisible(True)
        self._tool_cluster_layout.addWidget(card)
        _fade_in_widget(card)
        return card

    def get_tool_card(self, tool_call_id: str) -> "ToolCallCard | None":
        if self._compact_tools:
            return None
        return self._tool_cards.get(tool_call_id)

    def add_footer_widget(self, w: QWidget) -> None:
        self._footer.addWidget(w)
        _fade_in_widget(w)

    def finalize_content(self) -> None:
        """Replace the streaming label with a rich layout that renders code
        blocks as CodeBlockCard widgets instead of inline HTML pre blocks."""
        text = self._content_label.text_buffer()
        if not text:
            return

        # If no fenced code blocks, fall back to the old inline HTML render
        if not _CODE_FENCE_RE.search(text):
            html = _render_markdown_with_code(text)
            self._content_label.setTextFormat(Qt.TextFormat.RichText)
            self._content_label.setText(html)
            return

        # Parse into segments: list of (type, content, language)
        segments = self._parse_content(text)

        # Build a container widget to replace the streaming label
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(8)

        for seg_type, content, lang in segments:
            if seg_type == "text":
                if content.strip():
                    lbl = QLabel()
                    lbl.setWordWrap(True)
                    lbl.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                    )
                    lbl.setTextFormat(Qt.TextFormat.RichText)
                    # Render markdown (there are no code fences left, so this
                    # will just process inline formatting)
                    html = _render_markdown_with_code(content)
                    lbl.setText(html)
                    lbl.setStyleSheet(f"color: {FG};")
                    container_layout.addWidget(lbl)
            elif seg_type == "code":
                card = CodeBlockCard(lang, content)
                container_layout.addWidget(card)
                if lang == "mermaid" and self._chat_view is not None:
                    self._chat_view.mermaid_detected.emit(content)

        # Swap the streaming label out for the rich container
        idx = self._outer.indexOf(self._content_label)
        if idx >= 0:
            self._content_label.stop_timer()
            self._content_label.hide()
            # Insert new container at the same position
            self._outer.insertWidget(idx, container)
            # Remove old label from layout (but keep the object — it still
            # owns the text buffer and is referenced by append_content etc.)
            self._outer.removeWidget(self._content_label)

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


class ToolCallCard(QFrame):
    """Inline card representing one tool call.

    Header: 📄 name(args)   [running|done|failed]
    Body (collapsed by default): args and result
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName("toolCard")
        self._name = name
        self._args_text = ""
        self._state = self.STATE_RUNNING
        self._result_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        self._header = QToolButton()
        self._header.setObjectName("sectionToggle")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {FG_DIM}; }} "
            f"QToolButton#sectionToggle:hover {{ color: {FG}; }}"
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        self._args_view = QPlainTextEdit()
        self._args_view.setReadOnly(True)
        self._args_view.setFont(_mono_font(9))
        self._args_view.setStyleSheet(
            f"background: {BG}; color: {FG_DIM}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        self._args_view.setMaximumHeight(160)
        body_layout.addWidget(self._args_view)

        self._result_view = QPlainTextEdit()
        self._result_view.setReadOnly(True)
        self._result_view.setFont(_mono_font(9))
        self._result_view.setStyleSheet(
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        self._result_view.setMaximumHeight(220)
        self._result_view.setVisible(False)
        body_layout.addWidget(self._result_view)

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh_header()

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "(running)",
            self.STATE_DONE: "(done)",
            self.STATE_FAILED: "(failed)",
        }[self._state]
        color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS_DIM,
            self.STATE_FAILED: DANGER,
        }[self._state]
        # Prefer a short args summary in the header for readability.
        summary = self._summarize_args()
        text = f"{chev} {self._name}({summary})  "
        self._header.setText(text)
        # Style the state suffix via a separate stylesheet snippet on the QToolButton:
        self._header.setText(text + state_str)
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {color}; }}"
        )

    def _summarize_args(self) -> str:
        if not self._args_text:
            return ""
        try:
            parsed = json.loads(self._args_text)
        except json.JSONDecodeError:
            return self._args_text[:60].replace("\n", " ")
        if isinstance(parsed, dict):
            for key in ("path", "pattern"):
                if key in parsed and isinstance(parsed[key], str):
                    return f'"{parsed[key]}"'
            return ", ".join(f"{k}=…" for k in parsed)
        return str(parsed)[:60]

    def append_args(self, fragment: str) -> None:
        self._args_text += fragment
        # Try to pretty-print, fall back to raw.
        try:
            pretty = json.dumps(json.loads(self._args_text), indent=2, ensure_ascii=False)
            self._args_view.setPlainText(pretty)
        except json.JSONDecodeError:
            self._args_view.setPlainText(self._args_text)
        self._refresh_header()

    def set_result(self, ok: bool, result_text: str) -> None:
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        self._result_text = result_text
        try:
            pretty = json.dumps(json.loads(result_text), indent=2, ensure_ascii=False)
            self._result_view.setPlainText(pretty)
        except json.JSONDecodeError:
            self._result_view.setPlainText(result_text)
        self._result_view.setVisible(True)
        if not ok:
            self._body.setVisible(True)  # auto-expand failed
        self._refresh_header()


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
        self._args_text = ""
        self._state = self.STATE_RUNNING
        self._content_key = "content" if name == "write_file" else "new_str"
        self._last_content_len = 0

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
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        body_layout.addWidget(self._code_view)

        # Raw args fallback (shown when JSON can't be parsed yet)
        self._raw_view = QPlainTextEdit()
        self._raw_view.setReadOnly(True)
        self._raw_view.setFont(_mono_font(9))
        self._raw_view.setStyleSheet(
            f"background: {BG}; color: {FG_MUTED}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        self._raw_view.setMaximumHeight(160)
        self._raw_view.setVisible(False)
        body_layout.addWidget(self._raw_view)

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

    def append_args(self, fragment: str) -> None:
        self._args_text += fragment
        # Try to parse JSON
        try:
            parsed = json.loads(self._args_text)
        except json.JSONDecodeError:
            # JSON incomplete — show raw args in dim style
            self._raw_view.setVisible(True)
            self._raw_view.setPlainText(self._args_text)
            # Also try a best-effort path extraction from partial JSON
            self._try_extract_partial_path()
            return

        # JSON parsed successfully — hide raw view
        self._raw_view.setVisible(False)

        # Extract path
        path = parsed.get("path", "")
        if path:
            self._path = path
            self._path_label.setText(f"📄 {path}")
            self._path_label.setVisible(True)
            self._refresh_header()

        # Extract code content
        content = parsed.get(self._content_key, "")
        if content:
            self._code_view.setPlainText(content)
            # Attempt Pygments highlighting
            self._highlight_code()
            self._auto_size_code_view()

        # Show the body on first successful parse
        if not self._body.isVisible():
            self._body.setVisible(True)

    def _try_extract_partial_path(self) -> None:
        """Best-effort path extraction from partial JSON."""
        import re
        m = re.search(r'"path"\s*:\s*"([^"]*)', self._args_text)
        if m:
            path = m.group(1)
            if path and not self._path:
                self._path = path
                self._path_label.setText(f"📄 {path}")
                self._path_label.setVisible(True)
                self._refresh_header()

    def _highlight_code(self) -> None:
        if not _HAVE_PYGMENTS or not self._path:
            return
        # Guess lexer from file extension
        ext = self._path.rsplit(".", 1)[-1].lower() if "." in self._path else ""
        try:
            if ext:
                lexer = get_lexer_by_name(ext)
            else:
                lexer = TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        formatter = HtmlFormatter(
            style="dracula",
            noclasses=True,
            nowrap=True,
            prestyles=(
                "background: transparent; border:none; "
                "font-family:Consolas,'Cascadia Mono',monospace; "
                "font-size:12px; line-height:1.4;"
            ),
        )
        code = self._code_view.toPlainText()
        try:
            highlighted = highlight(code, lexer, formatter)
            # Set as HTML in the code view via the underlying document
            doc = self._code_view.document()
            doc.setHtml(highlighted)
        except Exception:
            pass  # Fall back to plain text

    def _auto_size_code_view(self) -> None:
        doc = self._code_view.document()
        doc.setDocumentMargin(4)
        doc_height = doc.size().height() + 8  # small padding
        doc_height = max(60, min(doc_height, 400))  # clamp between 60-400
        self._code_view.setFixedHeight(int(doc_height))

    def set_result(self, ok: bool, result_text: str) -> None:
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        if not ok:
            # Auto-expand body on failure
            self._body.setVisible(True)
        self._refresh_header()


class CodeBlockCard(QFrame):
    """Read-only card displaying a single syntax-highlighted code block."""

    def __init__(self, language: str, code: str) -> None:
        super().__init__()
        self.setObjectName("codeBlockCard")
        # Subtle card styling — distinct background, rounded border
        self.setStyleSheet(
            f"QFrame#codeBlockCard {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Language header bar
        lang_display = language if language else "code"
        header = QLabel(f" {lang_display} ")
        header.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            f"font-size: 10px; padding: 3px 10px; background: {BG_ALT}; "
            f"border-top-left-radius: 6px; border-top-right-radius: 6px; "
            f"border-bottom: 1px solid {BORDER};"
        )
        layout.addWidget(header)

        # Code view
        code_view = QPlainTextEdit()
        code_view.setReadOnly(True)
        code_view.setFont(_mono_font(10))
        code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        code_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG}; border: none; "
            f"padding: 8px; border-radius: 4px; }}"
        )
        code_view.setPlainText(code)
        code_view.setMinimumHeight(40)
        code_view.setMaximumHeight(400)
        # Auto-resize to content height (clamped by min/max)
        code_view.document().setDocumentMargin(2)
        layout.addWidget(code_view)

        # Apply Pygments highlighting
        self._highlight(code_view, language, code)

    @staticmethod
    def _highlight(view: QPlainTextEdit, language: str, code: str) -> None:
        if not _HAVE_PYGMENTS:
            return
        try:
            if language:
                lexer = get_lexer_by_name(language)
            else:
                lexer = TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        formatter = HtmlFormatter(style="dracula", noclasses=True, nowrap=True)
        try:
            highlighted = highlight(code, lexer, formatter)
            doc = view.document()
            doc.setHtml(highlighted)
        except Exception:
            pass  # Falls back to plain text set by caller


class DiffCard(QFrame):
    """Read-only inline diff display, after the user has decided."""

    def __init__(self, rel_path: str, old: str, new: str, decision: str, is_new_file: bool) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title_color = {
            "approve": SUCCESS,
            "reject": DANGER,
            "reject_all": DANGER,
        }.get(decision, FG)
        verb = {
            "approve": "Applied",
            "reject": "Rejected",
            "reject_all": "Rejected (all writes in this turn)",
        }.get(decision, decision)
        verb_prefix = "Created" if (is_new_file and decision == "approve") else verb

        title = QLabel(f"{verb_prefix}: {rel_path}")
        title.setStyleSheet(f"color: {title_color}; font-weight: 600;")
        layout.addWidget(title)

        diff_view = QPlainTextEdit()
        diff_view.setReadOnly(True)
        diff_view.setFont(_mono_font(9))
        diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        diff_view.setMaximumHeight(360)
        diff_view.setStyleSheet(
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        if is_new_file:
            text = "\n".join(f"+{line}" for line in new.splitlines())
        else:
            text = render_unified_diff(old, new, rel_path) or "(no textual difference)"
        diff_view.setPlainText(text)
        self._highlight(diff_view)
        layout.addWidget(diff_view)

    @staticmethod
    def _highlight(view: QPlainTextEdit) -> None:
        from PySide6.QtGui import QColor
        doc = view.document()
        cursor = QTextCursor(doc)
        add_fmt = QTextCharFormat()
        add_fmt.setBackground(QColor(DIFF_ADD_BG))
        add_fmt.setForeground(QColor(SUCCESS))
        del_fmt = QTextCharFormat()
        del_fmt.setBackground(QColor(DIFF_DEL_BG))
        del_fmt.setForeground(QColor(DANGER))
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        while True:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            line = cursor.selectedText()
            if line.startswith("+"):
                cursor.setCharFormat(add_fmt)
            elif line.startswith("-"):
                cursor.setCharFormat(del_fmt)
            cursor.clearSelection()
            if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                break


class SpecCard(QFrame):
    """Worker dispatch spec — collapsible, with Dispatch/Edit/Cancel buttons.

    After dispatch, the buttons collapse into a status header and a "View Worker"
    button appears to open the pop-out WorkerWindow.
    """

    dispatch_clicked = Signal(str)  # tool_call_id (with current spec values)
    edit_clicked = Signal(str)
    cancel_clicked = Signal(str)
    view_worker_clicked = Signal(str)  # tool_call_id

    def __init__(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
    ) -> None:
        super().__init__()
        self.setObjectName("specCard")
        self._tool_call_id = tool_call_id
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._dispatched = False
        self._cancelled = False
        self._worker_running = False

        self.setStyleSheet(
            f"QFrame#specCard {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {ACCENT}; border-radius: 8px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(8)

        header = QLabel("⚡ Dispatch to Worker")
        header.setStyleSheet(f"color: {ACCENT}; font-weight: 700; font-size: 12px;")
        outer.addWidget(header)

        self._goal_label = QLabel(self._goal)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        outer.addWidget(self._goal_label)

        self._files_label = QLabel(self._format_files(self._files))
        self._files_label.setWordWrap(True)
        self._files_label.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            "font-size: 11px;"
        )
        outer.addWidget(self._files_label)

        # Spec body (collapsible if long).
        self._spec_label = QLabel(self._spec)
        self._spec_label.setWordWrap(True)
        self._spec_label.setStyleSheet(f"color: {FG};")
        self._spec_label.setTextFormat(Qt.TextFormat.PlainText)

        self._spec_section: _CollapsibleSection | None = None
        if self._spec.count("\n") > 6 or len(self._spec) > 600:
            section = _CollapsibleSection(
                "Spec", self._spec_label, start_open=False, prominent=False
            )
            self._spec_section = section
            outer.addWidget(section)
        else:
            outer.addWidget(self._spec_label)

        self._acceptance_label = QLabel(f"Acceptance: {self._acceptance}")
        self._acceptance_label.setWordWrap(True)
        self._acceptance_label.setStyleSheet(
            f"color: {FG_MUTED}; font-style: italic;"
        )
        outer.addWidget(self._acceptance_label)

        # Buttons row.
        self._buttons_row = QWidget()
        btn_layout = QHBoxLayout(self._buttons_row)
        btn_layout.setContentsMargins(0, 4, 0, 0)
        btn_layout.setSpacing(8)

        self._dispatch_btn = QPushButton("Dispatch")
        self._dispatch_btn.setObjectName("primary")
        self._dispatch_btn.clicked.connect(self._on_dispatch)
        btn_layout.addWidget(self._dispatch_btn)

        self._edit_btn = QPushButton("Edit Spec")
        self._edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._tool_call_id))
        btn_layout.addWidget(self._edit_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("danger")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn)

        btn_layout.addStretch(1)

        outer.addWidget(self._buttons_row)

        # "View Worker" button — hidden until dispatch.
        self._view_worker_btn = QPushButton("View Worker")
        self._view_worker_btn.setVisible(False)
        self._view_worker_btn.clicked.connect(
            lambda: self.view_worker_clicked.emit(self._tool_call_id)
        )
        outer.addWidget(self._view_worker_btn)

        # Status label, hidden until dispatch/cancel.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(False)
        outer.addWidget(self._status_label)

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _format_files(files: list[str]) -> str:
        if not files:
            return "(no files listed)"
        return "  ".join(f"• {p}" for p in files)

    def update_spec(
        self, goal: str, files: list[str], spec: str, acceptance: str
    ) -> None:
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._goal_label.setText(self._goal)
        self._files_label.setText(self._format_files(self._files))
        self._spec_label.setText(self._spec)
        self._acceptance_label.setText(f"Acceptance: {self._acceptance}")

    def current_spec(self) -> tuple[str, list[str], str, str]:
        return (self._goal, list(self._files), self._spec, self._acceptance)

    def tool_call_id(self) -> str:
        return self._tool_call_id

    # ---- button handlers -------------------------------------------------

    def _on_dispatch(self) -> None:
        self._dispatched = True
        self._worker_running = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Dispatched — worker running…")
        self._status_label.setVisible(True)
        self._view_worker_btn.setVisible(True)
        self.dispatch_clicked.emit(self._tool_call_id)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Cancelled.")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.cancel_clicked.emit(self._tool_call_id)

    def disable_buttons(self) -> None:
        self._dispatch_btn.setEnabled(False)
        self._edit_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)

    def worker_finished(self, ok: bool, summary: str) -> None:
        self._worker_running = False
        verb = "Completed" if ok else "Completed with errors"
        color = SUCCESS if ok else DANGER
        self._status_label.setText(verb)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        # Keep "View Worker" button visible for later review.


class TerminalCard(QFrame):
    """Collapsible card showing streaming terminal output from run_terminal_command.

    Header: "> $ command" with state indicator: (running), (done ✓), (failed ✗)
    Body: dark monospace output area that auto-scrolls.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, command: str) -> None:
        super().__init__()
        self.setObjectName("terminalCard")
        self._command = command
        self._state = self.STATE_RUNNING
        self._output_buf = ""

        self.setStyleSheet(
            f"QFrame#terminalCard {{"
            f"  background: {TERMINAL_BG};"
            f"  border: 1px solid rgba(255, 255, 255, 0.06);"
            f"  border-left: 3px solid {SUCCESS};"
            f"  border-radius: 8px;"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header toggle
        self._header = QToolButton()
        self._header.setObjectName("sectionToggle")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body: output view
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._output_view = QPlainTextEdit()
        self._output_view.setReadOnly(True)
        self._output_view.setFont(_mono_font(9))
        self._output_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._output_view.setStyleSheet(
            f"background: {TERMINAL_BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; "
            "font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
        )
        self._output_view.setMaximumHeight(400)
        body_layout.addWidget(self._output_view)

        self._body.setVisible(True)  # Open by default for streaming
        layout.addWidget(self._body)

        self._refresh_header()

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "(running)",
            self.STATE_DONE: "(done ✓)",
            self.STATE_FAILED: "(failed ✗)",
        }[self._state]
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
        }[self._state]
        self._header.setText(f"{chev} > $ {self._command}  {state_str}")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {state_color}; }}"
        )

    def set_command(self, command: str) -> None:
        """Update the command shown in the header."""
        if command and command != "...":
            self._command = command
            self._refresh_header()

    def append_output(self, text: str) -> None:
        """Append a chunk of stdout/stderr text and auto-scroll."""
        self._output_buf += text
        self._output_view.insertPlainText(text)
        # Auto-scroll to bottom
        sb = self._output_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_result(self, exit_code: int) -> None:
        """Set the final state based on the exit code."""
        self._state = self.STATE_DONE if exit_code == 0 else self.STATE_FAILED
        if exit_code != 0:
            # Auto-expand on failure
            self._body.setVisible(True)
        else:
            # Collapse on success (user can toggle to view)
            self._body.setVisible(False)
        self._refresh_header()


class ErrorCard(QFrame):
    retry_clicked = Signal()

    def __init__(self, title: str, message: str, show_retry: bool = False) -> None:
        super().__init__()
        self.setObjectName("errorCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet(f"color: {DANGER}; font-weight: 600;")
        layout.addWidget(head)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color: {FG};")
        layout.addWidget(body)

        if show_retry:
            btn_layout = QHBoxLayout()
            btn_layout.setContentsMargins(0, 4, 0, 0)
            self._retry_btn = QPushButton("Retry")
            self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._retry_btn.setStyleSheet(
                f"color: {FG}; background: {BG_ALT}; border: 1px solid {BORDER}; padding: 4px 12px; border-radius: 4px;"
            )
            self._retry_btn.clicked.connect(self._on_retry)
            btn_layout.addWidget(self._retry_btn)
            btn_layout.addStretch(1)
            layout.addLayout(btn_layout)

    def _on_retry(self) -> None:
        self._retry_btn.setEnabled(False)
        self._retry_btn.setText("Retrying...")
        self.retry_clicked.emit()


# -----------------------------------------------------------------------------
# ChatView
# -----------------------------------------------------------------------------


class ChatView(QScrollArea):
    """Vertical, scrollable column of cards."""

    retry_requested = Signal()
    mermaid_detected = Signal(str)  # emits the raw mermaid code

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(32)
        self._layout.addStretch(1)
        self.setWidget(container)

        self._current_assistant: AssistantCard | None = None
        self._current_aura: AuraWidget | None = None
        # Map tool_call_id -> the assistant card that owns it (for routing diff-after).
        self._tool_owner: dict[str, AssistantCard] = {}
        # Map dispatch tool_call_id -> SpecCard.
        self._spec_cards: dict[str, SpecCard] = {}
        # Map tool_call_id -> TerminalCard.
        self._terminal_cards: dict[str, TerminalCard] = {}
        self._empty_hint: QLabel | None = None
        self._scroll_anim: QPropertyAnimation | None = None
        self._compact_tools: bool = False
        self._compact_tool_names: dict[str, str] = {}
        self._show_empty_hint()

    # ---- container management --------------------------------------------

    def _add_card(self, w: QWidget) -> None:
        if self._empty_hint is not None:
            self._empty_hint.deleteLater()
            self._empty_hint = None
        # Insert before the trailing stretch.
        self._layout.insertWidget(self._layout.count() - 1, w)
        _fade_in_widget(w)
        self._scroll_to_bottom()

    def _is_at_bottom(self, threshold: int = 30) -> bool:
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() <= threshold

    def _scroll_to_bottom(self, force: bool = False) -> None:
        if not force and not self._is_at_bottom():
            return
        bar = self.verticalScrollBar()
        # Stop any in-flight smooth scroll
        if hasattr(self, '_scroll_anim') and self._scroll_anim is not None:
            self._scroll_anim.stop()
        self._scroll_anim = QPropertyAnimation(bar, b"value")
        self._scroll_anim.setDuration(150)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(bar.maximum())
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.start()

    def set_compact_tools(self, enabled: bool) -> None:
        self._compact_tools = enabled

    def _show_empty_hint(self) -> None:
        hint = QLabel(
            "Start by describing the bug, dragging in code, or pasting a screenshot."
        )
        hint.setStyleSheet(f"color: {FG_ITALIC}; font-style: italic;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.insertWidget(0, hint)
        self._empty_hint = hint

    # ---- mutation API -----------------------------------------------------

    def reset(self) -> None:
        # Strip everything except the trailing stretch.
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_assistant = None
        self._current_aura = None
        self._tool_owner.clear()
        self._spec_cards.clear()
        self._terminal_cards.clear()
        self._compact_tool_names.clear()
        self._empty_hint = None
        self._show_empty_hint()

    def add_user(self, text: str, image_b64s: list[str] | None = None) -> None:
        # Slight right inset on user cards so the conversation rhythm is visible at a glance —
        # not a chat-bubble alignment, just enough to feel like input vs. output.
        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(UserCard(text, image_b64s), 1)
        h.addSpacing(40)
        self._add_card(wrapper)
        self._current_assistant = None  # next assistant turn opens a new card

    def begin_assistant(self) -> AssistantCard:
        card = AssistantCard(compact_tools=self._compact_tools)
        card._chat_view = self
        self._current_assistant = card
        wrapper = AuraWidget(card, glow_color=ACCENT, glow_spread=16)
        self._current_aura = wrapper
        self._add_card(wrapper)
        wrapper.start_aura()
        wrapper.set_glow_state("thinking")
        return card

    def current_assistant(self) -> AssistantCard:
        if self._current_assistant is None:
            return self.begin_assistant()
        return self._current_assistant

    def append_reasoning(self, text: str) -> None:
        self.current_assistant().append_reasoning(text)
        if self._current_aura is not None:
            self._current_aura.set_glow_state("thinking")
        self._scroll_to_bottom()

    def append_content(self, text: str) -> None:
        ac = self.current_assistant()
        # The first content delta means reasoning is done.
        ac.reasoning_done()
        # On first content delta, ensure the glow is in "thinking" state
        # (important for planners that don't produce reasoning content).
        if not ac._content_label.isVisible() and self._current_aura is not None:
            self._current_aura.set_glow_state("thinking")
        ac.append_content(text)
        self._scroll_to_bottom(force=True)

    def add_tool_call(self, tool_call_id: str, name: str) -> None:
        if self._current_aura is not None:
            self._current_aura.set_glow_state("coding")
        if self._compact_tools:
            ac = self.current_assistant()
            ac.notify_compact_tool_start(name)
            self._compact_tool_names[tool_call_id] = name
            self._scroll_to_bottom()
            return
        ac = self.current_assistant()
        if name == "run_terminal_command":
            card = TerminalCard(command="...")
            self._terminal_cards[tool_call_id] = card
            if not ac._tool_cluster.isVisible():
                ac._tool_cluster.setVisible(True)
            ac._tool_cluster_layout.addWidget(card)
            _fade_in_widget(card)
            self._tool_owner[tool_call_id] = ac
            self._scroll_to_bottom()
            return
        ac.add_tool_card(tool_call_id, name)
        self._tool_owner[tool_call_id] = ac
        self._scroll_to_bottom()

    def append_tool_args(self, tool_call_id: str, fragment: str) -> None:
        if self._compact_tools:
            return
        # Check for terminal card first
        term_card = self._terminal_cards.get(tool_call_id)
        if term_card is not None:
            # Try to extract command from partial/complete JSON
            import re as _re
            m = _re.search(r'"command"\s*:\s*"([^"]*)', fragment)
            if m:
                cmd = m.group(1)
                if cmd and cmd != "...":
                    term_card.set_command(cmd)
            return
        ac = self._tool_owner.get(tool_call_id) or self.current_assistant()
        card = ac.get_tool_card(tool_call_id)
        if card is not None:
            card.append_args(fragment)

    def set_tool_result(self, tool_call_id: str, ok: bool, result_text: str) -> None:
        if self._compact_tools:
            name = self._compact_tool_names.pop(tool_call_id, "tool")
            ac = self.current_assistant()
            ac.notify_compact_tool_done(name)
            return
        # Check for terminal card first
        term_card = self._terminal_cards.get(tool_call_id)
        if term_card is not None:
            try:
                parsed = json.loads(result_text)
                exit_code = parsed.get("exit_code", -1)
                term_card.set_result(exit_code)
            except (json.JSONDecodeError, TypeError):
                term_card.set_result(-1)
            return
        ac = self._tool_owner.get(tool_call_id) or self.current_assistant()
        card = ac.get_tool_card(tool_call_id)
        if card is not None:
            card.set_result(ok, result_text)

    def append_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Append a chunk of stdout/stderr to the TerminalCard."""
        card = self._terminal_cards.get(tool_call_id)
        if card is not None:
            card.append_output(text)
        self._scroll_to_bottom()

    def add_diff_card(
        self,
        owner_tool_call_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        # Attach diff card to the assistant that owned the tool call.
        ac = self._tool_owner.get(owner_tool_call_id) or self.current_assistant()
        card = DiffCard(rel_path, old, new, decision, is_new_file)
        # Append as a footer under the assistant card.
        ac.add_footer_widget(card)
        self._scroll_to_bottom()

    def add_error(self, title: str, message: str, show_retry: bool = False) -> None:
        card = ErrorCard(title, message, show_retry=show_retry)
        if show_retry:
            card.retry_clicked.connect(self.retry_requested.emit)
        self._add_card(card)

    def assistant_done(self) -> None:
        ac = self._current_assistant
        if ac is None:
            return
        ac.finalize_content()
        # Stop the breathing glow — content is complete, no need to pulse anymore.
        if self._current_aura is not None:
            self._current_aura.stop_aura()

    def stop_current_aura(self) -> None:
        """Stop the breathing glow on the current assistant card without finalizing content."""
        if self._current_aura is not None:
            self._current_aura.stop_aura()

    # ---- spec card / worker dispatch ------------------------------------

    def add_spec_card(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
    ) -> SpecCard:
        existing = self._spec_cards.get(tool_call_id)
        if existing is not None:
            existing.update_spec(goal, files, spec, acceptance)
            return existing
        card = SpecCard(tool_call_id, goal, files, spec, acceptance)
        ac = self.current_assistant()
        ac.add_footer_widget(card)
        self._spec_cards[tool_call_id] = card
        self._scroll_to_bottom()
        return card

    def get_spec_card(self, tool_call_id: str) -> SpecCard | None:
        return self._spec_cards.get(tool_call_id)

    def add_worker_summary(
        self, tool_call_id: str, goal: str, ok: bool, summary: str
    ) -> None:
        """Add a summary card to the chat after a worker completes."""
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(
            f"QFrame#card {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {SUCCESS if ok else DANGER}; border-radius: 8px; }}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Header
        status_icon = "✅" if ok else "⚠️"
        header = QLabel(f"{status_icon} Worker completed")
        header.setStyleSheet(
            f"color: {SUCCESS if ok else DANGER}; font-weight: 700; font-size: 12px;"
        )
        layout.addWidget(header)

        # Goal (dim)
        if goal:
            goal_label = QLabel(goal)
            goal_label.setWordWrap(True)
            goal_label.setStyleSheet(f"color: {FG_DIM}; font-style: italic;")
            layout.addWidget(goal_label)

        # Summary
        if summary:
            body = QLabel(summary)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setStyleSheet(f"color: {FG};")
            layout.addWidget(body)

        self._add_card(card)
