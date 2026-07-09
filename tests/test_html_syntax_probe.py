"""Tests for HTMLSyntaxProbe."""

from __future__ import annotations

from unittest.mock import patch

from aura.syntax_probe.html_probe import HTMLSyntaxProbe

VALID_HTML = "<!DOCTYPE html><html><head><title>Test</title></head><body><p>Hello</p></body></html>"
INVALID_HTML = "<html><body><p>broken"


class TestHTMLSyntaxProbe:
    """Tests for HTMLSyntaxProbe."""

    # --- detect ---

    def test_detect_html_file(self) -> None:
        assert HTMLSyntaxProbe.detect("index.html") is True
        assert HTMLSyntaxProbe.detect("page.htm") is True
        assert HTMLSyntaxProbe.detect("sub/about.html") is True
        assert HTMLSyntaxProbe.detect("sub/about.htm") is True

    def test_detect_case_insensitive(self) -> None:
        assert HTMLSyntaxProbe.detect("INDEX.HTML") is True
        assert HTMLSyntaxProbe.detect("index.HTM") is True

    def test_detect_non_html_file(self) -> None:
        assert HTMLSyntaxProbe.detect("index.py") is False
        assert HTMLSyntaxProbe.detect("style.css") is False
        assert HTMLSyntaxProbe.detect("data.json") is False

    # --- valid HTML -> pass ---

    def test_valid_html_returns_pass(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        html_file = tmp_path / "valid.html"
        html_file.write_text(VALID_HTML)
        with patch(
            "aura.syntax_probe.html_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("pass", None, None, "")
            result = probe.check(tmp_path, "valid.html")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False

    # --- invalid HTML -> fail ---

    def test_invalid_html_returns_fail(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        html_file = tmp_path / "invalid.html"
        html_file.write_text(INVALID_HTML)
        with patch(
            "aura.syntax_probe.html_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 1, 5, "Syntax error at line 1")
            result = probe.check(tmp_path, "invalid.html")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 1
        assert result.column == 5
        assert "Syntax error" in result.error
        assert result.failure_class == "syntax_invalid"

    def test_invalid_html_preserves_line_and_column(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        html_file = tmp_path / "invalid2.html"
        html_file.write_text(INVALID_HTML)
        with patch(
            "aura.syntax_probe.html_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 3, 10, "Syntax error at line 3")
            result = probe.check(tmp_path, "invalid2.html")
        assert result.evidence == "fail"
        assert result.line == 3
        assert result.column == 10
        assert result.failure_class == "syntax_invalid"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.html")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- parse failure -> no_evidence ---

    def test_parse_failure_returns_no_evidence(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        html_file = tmp_path / "parse_fail.html"
        html_file.write_text(VALID_HTML)
        with patch(
            "aura.syntax_probe.html_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = (
                "no_evidence",
                None,
                None,
                "Parse failed: bad bytes",
            )
            result = probe.check(tmp_path, "parse_fail.html")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert "failed" in result.error

    # --- outside-workspace safety ---

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_html"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.html"
        outside_file.write_text(VALID_HTML)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = HTMLSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.html")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
