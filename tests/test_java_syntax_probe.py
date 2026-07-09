"""Tests for JavaSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.java_probe import JavaSyntaxProbe, _parse_javac_error

_JAVA_VALID = """\
public class Hello {
    public static void main(String[] args) {
        System.out.println("hello");
    }
}
"""

_JAVA_INVALID = """\
public class Hello {
    public static void main(String[] args) {
        System.out.println("hello"
    }
}
"""


class TestJavaSyntaxProbe:
    """Tests for JavaSyntaxProbe."""

    def _probe(self) -> JavaSyntaxProbe:
        return JavaSyntaxProbe()

    # --- detect ---

    def test_detect_java_file(self) -> None:
        assert JavaSyntaxProbe.detect("Hello.java") is True
        assert JavaSyntaxProbe.detect("src/com/example/Main.java") is True

    def test_detect_non_java_file(self) -> None:
        assert JavaSyntaxProbe.detect("main.py") is False
        assert JavaSyntaxProbe.detect("main.go") is False
        assert JavaSyntaxProbe.detect("main.cs") is False

    # --- valid Java -> pass ---

    def test_valid_java_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_VALID)
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_INVALID)
        stderr_msg = (
            "{file}:3: error: ')' expected\n"
            "   System.out.println(\"hello\"\n"
            "                       ^\n"
        ).format(file=str(java_file))
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 3
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    def test_syntax_error_with_column(self, tmp_path) -> None:
        """Javac sometimes includes column: <file>:<line>:<col>: error: ..."""
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_INVALID)
        stderr_msg = (
            "{file}:3:9: error: ')' expected\n"
        ).format(file=str(java_file))
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "fail"
        assert result.line == 3
        assert result.column == 9

    # --- missing javac -> no_evidence ---

    def test_missing_javac_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- unrelated diagnostic -> no_evidence ---

    def test_cannot_find_symbol_returns_no_evidence(self, tmp_path) -> None:
        """Classpath/symbol errors should not be classified as syntax failures."""
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_VALID)
        stderr_msg = (
            "{file}:3: error: cannot find symbol\n"
            "  symbol:   class Scanner\n"
            "  location: class Hello\n"
        ).format(file=str(java_file))
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "no_evidence"
        assert result.failed is False
        assert result.toolchain_available is True

    def test_package_does_not_exist_returns_no_evidence(self, tmp_path) -> None:
        """Package resolution errors should not be classified as syntax failures."""
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_VALID)
        stderr_msg = (
            "{file}:1: error: package com.example does not exist\n"
        ).format(file=str(java_file))
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "no_evidence"
        assert result.failed is False

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_java"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "Outside.java"
        outside_file.write_text(_JAVA_VALID)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "../outside_workspace.java")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.java")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        java_file = tmp_path / "Hello.java"
        java_file.write_text(_JAVA_VALID)
        with patch("shutil.which", return_value="/usr/bin/javac"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="javac -d", timeout=60,
             )):
            result = probe.check(tmp_path, "Hello.java")
        assert result.evidence == "no_evidence"

    # --- _parse_javac_error unit tests ---

    def test_parse_javac_error_returns_none_for_empty_output(self) -> None:
        assert _parse_javac_error("", "/tmp/Hello.java") is None

    def test_parse_javac_error_parses_standard_format(self) -> None:
        result = _parse_javac_error(
            "/tmp/Hello.java:3: error: ')' expected\n",
            "/tmp/Hello.java",
        )
        assert result is not None
        line, col, msg = result
        assert line == 3
        assert msg == "')' expected"
