from __future__ import annotations

import os
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

    def test_absolute_path_inside_workspace(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        py_file = tmp_path / "inside.py"
        py_file.write_text(textwrap.dedent("""\
            x = 1
        """))
        result = probe.check(tmp_path, str(py_file))
        assert result.evidence == "pass"
        assert result.ok is True

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        # Create a file in another directory outside tmp_path.
        outside_dir = tmp_path.parent / "_outside_tmp"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.py"
        outside_file.write_text(textwrap.dedent("""\
            x = 1
        """))
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            # Cleanup
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.py")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    def test_unreadable_file_returns_no_evidence(self, tmp_path) -> None:
        probe = PythonSyntaxProbe()
        # Passing a directory path instead of a file triggers the is_file() check
        # which returns no_evidence (not a syntax failure).
        result = probe.check(tmp_path, str(tmp_path))
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
