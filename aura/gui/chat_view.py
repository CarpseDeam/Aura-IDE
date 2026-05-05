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
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QPixmap, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.diff_dialog import render_unified_diff
from aura.gui.theme import (
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
    SUCCESS,
    SUCCESS_DIM,
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
            f'<pre style="background:{BG}; color:{FG}; '
            f'border:1px solid {BORDER}; border-radius:6px; padding:8px; '
            f'font-family:Consolas,\'Cascadia Mono\',monospace;">{escaped}</pre>'
        )
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    formatter = HtmlFormatter(
        style="monokai",
        noclasses=True,
        nowrap=False,
        prestyles=(
            f"background:{BG}; border:1px solid {BORDER}; border-radius:6px; "
            "padding:8px; font-family:Consolas,'Cascadia Mono',monospace; "
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
    f = QFont("Cascadia Mono, Consolas, Menlo, monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    f.setPointSize(pt)
    return f


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
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(4)

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
    """Word-wrapping label that grows as text is appended. Cheap, good enough for
    streaming display before the final markdown render."""

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

    def append(self, text: str) -> None:
        self._buf += text
        if self._italic:
            escaped = _html.escape(self._buf).replace("\n", "<br/>")
            self.setText(
                f'<div style="color: {FG_ITALIC}; line-height: 145%; font-style: italic;">'
                f"{escaped}</div>"
            )
        else:
            self.setText(_wrap_body_text(self._buf, FG))

    def text_buffer(self) -> str:
        return self._buf


class AssistantCard(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("assistantCard")
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(14, 10, 14, 12)
        self._outer.setSpacing(4)

        header = QLabel("Aura")
        header.setObjectName("assistantHeader")
        self._outer.addWidget(header)

        # Reasoning: lazy — created on first reasoning delta.
        self._reasoning_section: _CollapsibleSection | None = None
        self._reasoning_label: _StreamLabel | None = None

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
        if self._reasoning_label is None:
            self._reasoning_label = _StreamLabel(italic=True)
            section = _CollapsibleSection(
                "Thinking…", self._reasoning_label, start_open=True, prominent=True
            )
            self._reasoning_section = section
            # Insert reasoning at the top, after header (index 1).
            self._outer.insertWidget(1, section)
        self._reasoning_label.append(text)

    def reasoning_done(self) -> None:
        if self._reasoning_section is not None:
            self._reasoning_section.set_title("Thinking")
            # If content has started, collapse reasoning by default.
            if self._content_label.isVisible():
                self._reasoning_section.set_open(False)

    def append_content(self, text: str) -> None:
        if not self._content_label.isVisible():
            self._content_label.setVisible(True)
            # First content -> collapse reasoning.
            if self._reasoning_section is not None:
                self._reasoning_section.set_open(False)
        self._content_label.append(text)

    def add_tool_card(self, tool_call_id: str, name: str) -> "ToolCallCard":
        card = ToolCallCard(name)
        self._tool_cards[tool_call_id] = card
        if not self._tool_cluster.isVisible():
            self._tool_cluster.setVisible(True)
        self._tool_cluster_layout.addWidget(card)
        return card

    def get_tool_card(self, tool_call_id: str) -> "ToolCallCard | None":
        return self._tool_cards.get(tool_call_id)

    def add_footer_widget(self, w: QWidget) -> None:
        self._footer.addWidget(w)


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
        layout.setContentsMargins(10, 6, 10, 6)
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


class DiffCard(QFrame):
    """Read-only inline diff display, after the user has decided."""

    def __init__(self, rel_path: str, old: str, new: str, decision: str, is_new_file: bool) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
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
        diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        diff_view.setMaximumHeight(360)
        diff_view.setStyleSheet(
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        if is_new_file:
            text = f"--- /dev/null\n+++ b/{rel_path}\n"
            for line in new.splitlines():
                text += f"+{line}\n"
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
        head_fmt = QTextCharFormat()
        head_fmt.setForeground(QColor(FG_DIM))
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        while True:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            line = cursor.selectedText()
            if line.startswith("+") and not line.startswith("+++"):
                cursor.setCharFormat(add_fmt)
            elif line.startswith("-") and not line.startswith("---"):
                cursor.setCharFormat(del_fmt)
            elif line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
                cursor.setCharFormat(head_fmt)
            cursor.clearSelection()
            if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                break


class ErrorCard(QFrame):
    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.setObjectName("errorCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet(f"color: {DANGER}; font-weight: 600;")
        layout.addWidget(head)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color: {FG};")
        layout.addWidget(body)


# -----------------------------------------------------------------------------
# ChatView
# -----------------------------------------------------------------------------


class ChatView(QScrollArea):
    """Vertical, scrollable column of cards."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(28)
        self._layout.addStretch(1)
        self.setWidget(container)

        self._current_assistant: AssistantCard | None = None
        # Map tool_call_id -> the assistant card that owns it (for routing diff-after).
        self._tool_owner: dict[str, AssistantCard] = {}
        self._empty_hint: QLabel | None = None
        self._show_empty_hint()

    # ---- container management --------------------------------------------

    def _add_card(self, w: QWidget) -> None:
        if self._empty_hint is not None:
            self._empty_hint.deleteLater()
            self._empty_hint = None
        # Insert before the trailing stretch.
        self._layout.insertWidget(self._layout.count() - 1, w)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

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
        self._tool_owner.clear()
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
        card = AssistantCard()
        self._current_assistant = card
        self._add_card(card)
        return card

    def current_assistant(self) -> AssistantCard:
        if self._current_assistant is None:
            return self.begin_assistant()
        return self._current_assistant

    def append_reasoning(self, text: str) -> None:
        self.current_assistant().append_reasoning(text)
        self._scroll_to_bottom()

    def append_content(self, text: str) -> None:
        ac = self.current_assistant()
        # The first content delta means reasoning is done.
        ac.reasoning_done()
        ac.append_content(text)
        self._scroll_to_bottom()

    def add_tool_call(self, tool_call_id: str, name: str) -> None:
        ac = self.current_assistant()
        ac.add_tool_card(tool_call_id, name)
        self._tool_owner[tool_call_id] = ac
        self._scroll_to_bottom()

    def append_tool_args(self, tool_call_id: str, fragment: str) -> None:
        ac = self._tool_owner.get(tool_call_id) or self.current_assistant()
        card = ac.get_tool_card(tool_call_id)
        if card is not None:
            card.append_args(fragment)

    def set_tool_result(self, tool_call_id: str, ok: bool, result_text: str) -> None:
        ac = self._tool_owner.get(tool_call_id) or self.current_assistant()
        card = ac.get_tool_card(tool_call_id)
        if card is not None:
            card.set_result(ok, result_text)

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

    def add_error(self, title: str, message: str) -> None:
        self._add_card(ErrorCard(title, message))

    def assistant_done(self) -> None:
        ac = self._current_assistant
        if ac is None:
            return
        text = ac._content_label.text_buffer()
        if not text:
            return
        html = _render_markdown_with_code(text)
        ac._content_label.setTextFormat(Qt.TextFormat.RichText)
        ac._content_label.setText(html)
