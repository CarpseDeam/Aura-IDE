from __future__ import annotations

from aura.syntax_probe.json_probe import JSONSyntaxProbe

VALID_JSON = """\
{
    "name": "test",
    "value": 42
}
"""

INVALID_JSON = """\
{
    "name": "test",
    "value": 42,
"""


class TestJSONSyntaxProbe:
    """Tests for JSONSyntaxProbe."""

    def test_detect_json_file(self) -> None:
        assert JSONSyntaxProbe.detect("data.json") is True
        assert JSONSyntaxProbe.detect("config.json") is True

    def test_detect_non_json_file(self) -> None:
        assert JSONSyntaxProbe.detect("data.yaml") is False
        assert JSONSyntaxProbe.detect("data.toml") is False
        assert JSONSyntaxProbe.detect("data.py") is False

    def test_valid_json_returns_pass(self, tmp_path) -> None:
        probe = JSONSyntaxProbe()
        json_file = tmp_path / "valid.json"
        json_file.write_text(VALID_JSON)
        result = probe.check(tmp_path, "valid.json")
        assert result.ok is True
        assert result.evidence == "pass"
        assert result.failed is False

    def test_invalid_json_returns_fail(self, tmp_path) -> None:
        probe = JSONSyntaxProbe()
        json_file = tmp_path / "invalid.json"
        json_file.write_text(INVALID_JSON)
        result = probe.check(tmp_path, "invalid.json")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.failure_class == "syntax_invalid"
        assert result.line is not None
        assert result.column is not None

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = JSONSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.json")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = JSONSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_json"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.json"
        outside_file.write_text(VALID_JSON)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = JSONSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.json")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
