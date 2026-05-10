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
            f'font-family:\'Geist Mono\',\'JetBrains Mono\',monospace; '
            f'white-space: pre-wrap;\">{escaped}</pre>'
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
            "font-size:12px; white-space:pre-wrap;"
        ),
    )
    return highlight(code, lexer, formatter)


def _render_markdown_with_code(text: str, color: str | None = None, italic: bool = False) -> str:
    """Render a markdown string to Qt-friendly HTML, swapping fenced code
    blocks for Pygments-highlighted HTML. 
    """
    if not text:
        return ""

    blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip().lower()
        code = match.group(2)
        idx = len(blocks)
        blocks.append(_render_code_block(lang, code))
        return f"\n\nAURACODEPLACEHOLDER{idx}ENDAURA\n\n"

    intermediate = _CODE_FENCE_RE.sub(stash, text)

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

    # 1. Strip hardcoded colors that QTextDocument bakes into elements.
    html = re.sub(r"color\s*:\s*#[0-9a-fA-F]+\s*;?", "", html)
    
    # 2. Strip hardcoded font-family/size from <body> and <p> so QSS takes over.
    html = re.sub(r"font-family\s*:\s*'[^']+'\s*;?", "", html)
    html = re.sub(r"font-size\s*:\s*[0-9]+pt\s*;?", "", html)
    
    # 3. Tighten up paragraph spacing (Qt defaults to 6px/6px which is loose).
    html = html.replace("margin-top:6px; margin-bottom:6px;", "margin-top:2px; margin-bottom:2px;")

    for i, block in enumerate(blocks):
        token = f"AURACODEPLACEHOLDER{i}ENDAURA"
        wrapped = re.compile(r"<p[^>]*>\s*" + re.escape(token) + r"\s*</p>")
        if wrapped.search(html):
            html = wrapped.sub(block, html, count=1)
        else:
            html = html.replace(token, block, 1)

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

    # Inject body styles.
    final_color = color if color else FG
    style_payload = f"color: {final_color}; line-height: 140%;"
    if italic:
        style_payload += " font-style: italic;"
    
    if 'style="' in html.lower():
        html = re.sub(r'(<body[^>]*style=")', r'\1' + style_payload + " ", html, count=1, flags=re.IGNORECASE)
    else:
        html = html.replace("<body ", f'<body style="{style_payload}" ', 1)
        
    return html

