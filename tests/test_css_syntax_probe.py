"""Tests for CSSSyntaxProbe."""

from __future__ import annotations

from unittest.mock import patch

from aura.syntax_probe.css_probe import CSSSyntaxProbe

VALID_CSS = "body { color: red; }"
INVALID_CSS = "body { color: red;"


class TestCSSSyntaxProbe:
    """Tests for CSSSyntaxProbe."""

    # --- detect ---

    def test_detect_css_file(self) -> None:
        assert CSSSyntaxProbe.detect("style.css") is True
        assert CSSSyntaxProbe.detect("sub/layout.css") is True
        assert CSSSyntaxProbe.detect("theme.css") is True

    def test_detect_case_insensitive(self) -> None:
        assert CSSSyntaxProbe.detect("STYLE.CSS") is True
        assert CSSSyntaxProbe.detect("Style.Css") is True

    def test_detect_non_css_file(self) -> None:
        assert CSSSyntaxProbe.detect("index.html") is False
        assert CSSSyntaxProbe.detect("script.js") is False
        assert CSSSyntaxProbe.detect("data.json") is False

    # --- valid CSS -> pass ---

    def test_valid_css_returns_pass(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        css_file = tmp_path / "valid.css"
        css_file.write_text(VALID_CSS)
        with patch(
            "aura.syntax_probe.css_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("pass", None, None, "")
            result = probe.check(tmp_path, "valid.css")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False

    # --- invalid CSS -> fail ---

    def test_invalid_css_returns_fail(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        css_file = tmp_path / "invalid.css"
        css_file.write_text(INVALID_CSS)
        with patch(
            "aura.syntax_probe.css_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 1, 5, "Syntax error at line 1")
            result = probe.check(tmp_path, "invalid.css")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 1
        assert result.column == 5
        assert "Syntax error" in result.error
        assert result.failure_class == "syntax_invalid"

    def test_invalid_css_preserves_line_and_column(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        css_file = tmp_path / "invalid2.css"
        css_file.write_text(INVALID_CSS)
        with patch(
            "aura.syntax_probe.css_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 3, 10, "Syntax error at line 3")
            result = probe.check(tmp_path, "invalid2.css")
        assert result.evidence == "fail"
        assert result.line == 3
        assert result.column == 10
        assert result.failure_class == "syntax_invalid"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.css")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- parse failure -> no_evidence ---

    def test_parse_failure_returns_no_evidence(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        css_file = tmp_path / "parse_fail.css"
        css_file.write_text(VALID_CSS)
        with patch(
            "aura.syntax_probe.css_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = (
                "no_evidence",
                None,
                None,
                "Parse failed: bad bytes",
            )
            result = probe.check(tmp_path, "parse_fail.css")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert "failed" in result.error

    # --- outside-workspace safety ---

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_css"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.css"
        outside_file.write_text(VALID_CSS)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = CSSSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.css")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
