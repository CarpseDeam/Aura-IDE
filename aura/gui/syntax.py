from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.util import ClassNotFound
from pygments.styles import get_style_by_name


def language_from_path(path: str) -> str:
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
        super().__init__(parent)
        try:
            self._style = get_style_by_name("dracula")
        except ClassNotFound:
            # Fallback if dracula is missing for some reason
            from pygments.styles import get_all_styles
            available = list(get_all_styles())
            self._style = get_style_by_name(available[0] if available else "default")
            
        self._format_cache: dict[tuple, QTextCharFormat] = {}
        self._lexer = TextLexer()
        self.set_language(language)

    def set_language(self, language: str) -> None:
        """Update the lexer based on the language name or file extension."""
        try:
            if language:
                # Try to get lexer by name or alias
                self._lexer = get_lexer_by_name(language)
            else:
                self._lexer = TextLexer()
        except ClassNotFound:
            # If not found, fall back to plain text
            self._lexer = TextLexer()
        
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
