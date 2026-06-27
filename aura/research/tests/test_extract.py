"""Tests for aura.research.extract — text normalisation helpers."""

from __future__ import annotations

from aura.research.extract import ParsedPage, normalize_text


class TestNormalizeText:
    """Verify normalize_text strips trailing whitespace and collapses blank lines."""

    def test_strips_trailing_whitespace(self) -> None:
        raw = "hello   \nworld  \n"
        result = normalize_text(raw)
        # Trailing blank line from the final \\n is preserved
        assert result == "hello\nworld\n"

    def test_collapses_blank_lines(self) -> None:
        raw = "a\n\n\n\nb\n\nc"
        result = normalize_text(raw)
        assert result == "a\n\nb\n\nc"

    def test_empty_string(self) -> None:
        assert normalize_text("") == ""


class TestParsedPage:
    """Verify ParsedPage dataclass construction."""

    def test_defaults(self) -> None:
        page = ParsedPage()
        assert page.url == ""
        assert page.title == ""
        assert page.clean_text == ""
        assert page.links == []

    def test_with_values(self) -> None:
        page = ParsedPage(
            url="https://example.com",
            title="Example",
            clean_text="some text",
            links=[("click here", "https://example.com/page")],
        )
        assert page.url == "https://example.com"
        assert page.title == "Example"
        assert page.clean_text == "some text"
        assert page.links == [("click here", "https://example.com/page")]
