from __future__ import annotations

import textwrap

from aura.syntax_probe.python_probe import PythonSyntaxProbe


class TestPythonSyntaxProbe:
    """Tests for PythonSyntaxProbe."""

    def test_valid_python_returns_pass(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        py_file = tmp_path / "valid.py"
        py_file.write_text(textwrap.dedent("""\
            x = 42
            print(x)
        """))
        result = probe.check(tmp_path, "valid.py")
        assert result.ok is True
        assert result.evidence == "pass"
        assert result.failed is False

    def test_invalid_python_returns_fail(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        py_file = tmp_path / "invalid.py"
        py_file.write_text(textwrap.dedent("""\
            x = 42
            print(x
        """))
        result = probe.check(tmp_path, "invalid.py")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.failure_class == "syntax_invalid"
        assert isinstance(result.line, int)

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.py")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
