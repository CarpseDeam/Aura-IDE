"""Markdown-to-HTML rendering with Pygments code block highlighting."""
from __future__ import annotations

import html as _html
import re

from PySide6.QtGui import QTextDocument

from aura.gui.cards._helpers import _CODE_FENCE_RE, _HAVE_PYGMENTS
from aura.gui.theme import BG_ALT, FG


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
        from pygments.lexers import TextLexer, get_lexer_by_name
        from pygments.util import ClassNotFound
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        from pygments.lexers import TextLexer
        lexer = TextLexer()
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
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

    # Stash inline code (single backticks) so markdown won't touch it
    _INLINE_CODE_RE = re.compile(r"`([^`]+)`")
    inline_blocks: list[str] = []

    def _stash_inline(match: re.Match[str]) -> str:
        code = match.group(1)
        idx = len(inline_blocks)
        inline_blocks.append(code)
        return f"AURAICODESTART{idx}AURAICODEEND"

    intermediate = _INLINE_CODE_RE.sub(_stash_inline, intermediate)

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

    # Replace inline code placeholders with styled spans
    for i, code_text in enumerate(inline_blocks):
        token = f"AURAICODESTART{i}AURAICODEEND"
        escaped = _html.escape(code_text)
        replacement = (
            f'<span style="background-color: {BG_ALT}; '
            f'color: {FG}; '
            f"font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            f'font-size: 0.95em; padding: 1px 4px; border-radius: 3px;">'
            f'{escaped}</span>'
        )
        html = html.replace(token, replacement, 1)

    # Inject body color + line-height directly into the <body> tag of the
    # full HTML document produced by QTextDocument.toHtml().
    # We must merge our styles into the existing style attribute if present.
    style_payload = f"color: {FG}; line-height: 145%;"
    if 'style="' in html.lower():
        html = re.sub(r'(<body[^>]*style=")', r'\1' + style_payload + " ", html, count=1, flags=re.IGNORECASE)
    else:
        html = html.replace("<body ", f'<body style="{style_payload}" ', 1)
    return html
