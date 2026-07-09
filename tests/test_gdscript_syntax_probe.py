"""Tests for GDScriptSyntaxProbe using tree-sitter.

All tree-sitter calls are mocked — no GDScript grammar installation required.
"""

from __future__ import annotations

from unittest.mock import patch

from aura.syntax_probe.gdscript_probe import GDScriptSyntaxProbe

VALID_GDSCRIPT = """\
extends Node

func _ready() -> void:
    print("Hello, world!")
"""

INVALID_GDSCRIPT = """\
extends Node

func _ready() -> void:
    print("Hello, world!"
"""


class TestGDScriptSyntaxProbe:
    """Tests for GDScriptSyntaxProbe."""

    def test_detect_gd_file(self) -> None:
        assert GDScriptSyntaxProbe.detect("script.gd") is True
        assert GDScriptSyntaxProbe.detect("res://player.gd") is True

    def test_detect_non_gd_file(self) -> None:
        assert GDScriptSyntaxProbe.detect("script.py") is False
        assert GDScriptSyntaxProbe.detect("scene.tscn") is False
        assert GDScriptSyntaxProbe.detect("script.sh") is False

    def test_valid_gdscript_returns_pass(self, tmp_path) -> None:
        probe = GDScriptSyntaxProbe()
        gd_file = tmp_path / "valid.gd"
        gd_file.write_text(VALID_GDSCRIPT)

        with patch(
            "aura.syntax_probe.gdscript_probe._tree_sitter_check",
            return_value=("pass", None, None, ""),
        ) as mock_check:
            result = probe.check(tmp_path, "valid.gd")

        mock_check.assert_called_once()
        assert result.ok is True
        assert result.evidence == "pass"
        assert result.failed is False

    def test_invalid_gdscript_returns_fail(self, tmp_path) -> None:
        probe = GDScriptSyntaxProbe()
        gd_file = tmp_path / "invalid.gd"
        gd_file.write_text(INVALID_GDSCRIPT)

        with patch(
            "aura.syntax_probe.gdscript_probe._tree_sitter_check",
            return_value=("fail", 5, 10, "Syntax error at line 5"),
        ) as mock_check:
            result = probe.check(tmp_path, "invalid.gd")

        mock_check.assert_called_once()
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.failure_class == "syntax_invalid"
        assert result.line == 5
        assert result.column == 10
        assert "Syntax error" in result.error

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = GDScriptSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.gd")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = GDScriptSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_gdscript"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.gd"
        outside_file.write_text(VALID_GDSCRIPT)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = GDScriptSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.gd")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_tree_sitter_grammar_unavailable_returns_no_evidence(
        self, tmp_path,
    ) -> None:
        """Simulate tree-sitter grammar not being available."""
        probe = GDScriptSyntaxProbe()
        gd_file = tmp_path / "unavailable.gd"
        gd_file.write_text(VALID_GDSCRIPT)

        with patch(
            "aura.syntax_probe.gdscript_probe._tree_sitter_check",
            return_value=("no_evidence", None, None, "Language not available: gdscript"),
        ) as mock_check:
            result = probe.check(tmp_path, "unavailable.gd")

        mock_check.assert_called_once()
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
