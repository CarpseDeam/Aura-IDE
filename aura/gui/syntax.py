from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.util import ClassNotFound
from pygments.styles import get_style_by_name

from aura.gui.theme import DIFF_ADD_BG, DIFF_DEL_BG, FG_DIM, SUCCESS, DANGER

# Global cache to avoid redundant lookups during bulk replay
_STYLE_CACHE: dict[str, Any] = {}
_LEXER_CACHE: dict[str, Any] = {}


def _get_cached_style(name: str = "dracula") -> Any:
    if name not in _STYLE_CACHE:
        try:
            _STYLE_CACHE[name] = get_style_by_name(name)
        except ClassNotFound:
            from pygments.styles import get_all_styles
            available = list(get_all_styles())
            fallback = available[0] if available else "default"
            if fallback not in _STYLE_CACHE:
                _STYLE_CACHE[fallback] = get_style_by_name(fallback)
            _STYLE_CACHE[name] = _STYLE_CACHE[fallback]
    return _STYLE_CACHE[name]


def _get_cached_lexer(language: str) -> Any:
    if not language:
        return TextLexer()
    if language not in _LEXER_CACHE:
        try:
            _LEXER_CACHE[language] = get_lexer_by_name(language)
        except ClassNotFound:
            _LEXER_CACHE[language] = TextLexer()
    return _LEXER_CACHE[language]


def language_from_path(path: str) -> str:
    # ... (same as before)
    """Return a pygments-compatible language identifier from a file path."""
    ext = Path(path).suffix.lower()
    lang_map = {
        ".html": "html", ".svg": "svg", ".md": "markdown",
        ".py": "python", ".pyi": "python", ".gd": "python",
        ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
        ".jsx": "jsx", ".css": "css", ".scss": "scss", ".json": "json",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".rs": "rust", ".go": "go", ".c": "c", ".cpp": "cpp", ".h": "c",
        ".hpp": "cpp", ".java": "java", ".kt": "kotlin", ".swift": "swift",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash",
        ".txt": "text", ".cfg": "ini", ".ini": "ini",
        ".xml": "xml", ".sql": "sql", ".r": "r",
    }
    return lang_map.get(ext, "text")


class PygmentsHighlighter(QSyntaxHighlighter):
    """
    A QSyntaxHighlighter that uses Pygments to highlight code blocks.
    Inherits native highlighting to avoid 30fps HTML rebuilds.
    """

    def __init__(self, parent, language: str = "text"):
        self._style = _get_cached_style("dracula")
        self._format_cache: dict[tuple, QTextCharFormat] = {}
        self._lexer = _get_cached_lexer(language)
        super().__init__(parent)

    def set_language(self, language: str) -> None:
        """Update the lexer based on the language name or file extension."""
        self._lexer = _get_cached_lexer(language)
        # Trigger re-highlighting of the entire document
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        """Apply highlighting to a block of text using the current lexer."""
        if not text:
            return

        # QSyntaxHighlighter processes block by block. 
        # For most lexers, this is acceptable for a chat UI.
        offset = 0
        for token, content in self._lexer.get_tokens(text):
            length = len(content)
            if length == 0:
                continue
            fmt = self._get_format(token)
            self.setFormat(offset, length, fmt)
            offset += length

    def _get_format(self, token) -> QTextCharFormat:
        """Get or create a QTextCharFormat for a given Pygments token."""
        if token in self._format_cache:
            return self._format_cache[token]

        style_attr = self._style.style_for_token(token)
        fmt = QTextCharFormat()

        if style_attr["color"]:
            fmt.setForeground(QColor(f"#{style_attr['color']}"))
        if style_attr["bgcolor"]:
            fmt.setBackground(QColor(f"#{style_attr['bgcolor']}"))
        if style_attr["bold"]:
            fmt.setFontWeight(QFont.Weight.Bold)
        if style_attr["italic"]:
            fmt.setFontItalic(True)
        if style_attr["underline"]:
            fmt.setFontUnderline(True)

        self._format_cache[token] = fmt
        return fmt


class DiffHighlighter(PygmentsHighlighter):
    """QSyntaxHighlighter for unified diff views.

    Strips the leading +/-/space prefix before feeding text to the Pygments
    lexer, then applies token colours starting at offset 1.  The whole line
    gets a diff background (green for +, red for -) and the prefix character
    itself is coloured with SUCCESS / DANGER.
    """

    def __init__(self, parent, language: str = "text"):
        # Whole-line background only (token foregrounds shine through)
        self._add_bg = QTextCharFormat()
        self._add_bg.setBackground(QColor(DIFF_ADD_BG))
        self._del_bg = QTextCharFormat()
        self._del_bg.setBackground(QColor(DIFF_DEL_BG))

        # Prefix character formats
        self._add_prefix = QTextCharFormat()
        self._add_prefix.setForeground(QColor(SUCCESS))
        self._add_prefix.setBackground(QColor(DIFF_ADD_BG))
        self._del_prefix = QTextCharFormat()
        self._del_prefix.setForeground(QColor(DANGER))
        self._del_prefix.setBackground(QColor(DIFF_DEL_BG))

        # Hunk header format
        self._hunk_fmt = QTextCharFormat()
        self._hunk_fmt.setForeground(QColor(FG_DIM))

        super().__init__(parent, language)

    def highlightBlock(self, text: str) -> None:
        if not text:
            return

        # Hunk headers (e.g. "@@ -1,4 +1,5 @@")
        if text.startswith("@@"):
            self.setFormat(0, len(text), self._hunk_fmt)
            return

        prefix = text[0] if text else ""
        if prefix not in ("+", "-", " "):
            # Fallback: no diff prefix — use normal pygments highlighting
            super().highlightBlock(text)
            return

        code = text[1:]

        # Whole-line diff background
        if prefix == "+":
            self.setFormat(0, len(text), self._add_bg)
            self.setFormat(0, 1, self._add_prefix)
        elif prefix == "-":
            self.setFormat(0, len(text), self._del_bg)
            self.setFormat(0, 1, self._del_prefix)

        # Token-level syntax highlighting on the code portion (offset = 1)
        if code:
            offset = 1
            for token, content in self._lexer.get_tokens(code):
                length = len(content)
                if length == 0:
                    continue
                fmt = self._get_format(token)
                self.setFormat(offset, length, fmt)
                offset += length
