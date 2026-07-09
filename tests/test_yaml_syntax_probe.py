from __future__ import annotations

from unittest.mock import patch

from aura.syntax_probe.yaml_probe import YAMLSyntaxProbe

VALID_YAML = """\
name: test
value: 42
"""

INVALID_YAML = """\
name: test
  nested: bad
value: 42
"""


class TestYAMLSyntaxProbe:
    """Tests for YAMLSyntaxProbe."""

    def test_detect_yaml_file(self) -> None:
        assert YAMLSyntaxProbe.detect("config.yaml") is True
        assert YAMLSyntaxProbe.detect("config.yml") is True

    def test_detect_non_yaml_file(self) -> None:
        assert YAMLSyntaxProbe.detect("config.json") is False
        assert YAMLSyntaxProbe.detect("config.toml") is False
        assert YAMLSyntaxProbe.detect("config.py") is False

    def test_valid_yaml_returns_pass(self, tmp_path) -> None:
        probe = YAMLSyntaxProbe()
        yaml_file = tmp_path / "valid.yaml"
        yaml_file.write_text(VALID_YAML)
        result = probe.check(tmp_path, "valid.yaml")
        assert result.ok is True
        assert result.evidence == "pass"
        assert result.failed is False

    def test_invalid_yaml_returns_fail(self, tmp_path) -> None:
        probe = YAMLSyntaxProbe()
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text(INVALID_YAML)
        result = probe.check(tmp_path, "invalid.yaml")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.failure_class == "syntax_invalid"
        assert result.line is not None
        assert result.column is not None

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = YAMLSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.yaml")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = YAMLSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_yaml"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.yaml"
        outside_file.write_text(VALID_YAML)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = YAMLSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.yaml")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_yaml_unavailable_returns_no_evidence(self, tmp_path) -> None:
        """When PyYAML is not available, probe returns no_evidence."""
        probe = YAMLSyntaxProbe()
        yaml_file = tmp_path / "valid.yaml"
        yaml_file.write_text(VALID_YAML)
        with patch("aura.syntax_probe.yaml_probe._YAML_AVAILABLE", False):
            result = probe.check(tmp_path, "valid.yaml")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
