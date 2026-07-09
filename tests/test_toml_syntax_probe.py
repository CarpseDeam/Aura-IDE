from __future__ import annotations

from aura.syntax_probe.toml_probe import TOMLSyntaxProbe

VALID_TOML = """\
[package]
name = "test"
version = "0.1.0"
"""

INVALID_TOML = """\
[package
name = "test"
"""


class TestTOMLSyntaxProbe:
    """Tests for TOMLSyntaxProbe."""

    def test_detect_toml_file(self) -> None:
        assert TOMLSyntaxProbe.detect("config.toml") is True
        assert TOMLSyntaxProbe.detect("Cargo.toml") is True

    def test_detect_non_toml_file(self) -> None:
        assert TOMLSyntaxProbe.detect("config.yaml") is False
        assert TOMLSyntaxProbe.detect("config.json") is False
        assert TOMLSyntaxProbe.detect("config.py") is False

    def test_valid_toml_returns_pass(self, tmp_path) -> None:
        probe = TOMLSyntaxProbe()
        toml_file = tmp_path / "valid.toml"
        toml_file.write_text(VALID_TOML)
        result = probe.check(tmp_path, "valid.toml")
        assert result.ok is True
        assert result.evidence == "pass"
        assert result.failed is False

    def test_invalid_toml_returns_fail(self, tmp_path) -> None:
        probe = TOMLSyntaxProbe()
        toml_file = tmp_path / "invalid.toml"
        toml_file.write_text(INVALID_TOML)
        result = probe.check(tmp_path, "invalid.toml")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.failure_class == "syntax_invalid"
        assert result.line is not None
        assert result.column is not None

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = TOMLSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.toml")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = TOMLSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_toml"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.toml"
        outside_file.write_text(VALID_TOML)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = TOMLSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.toml")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
