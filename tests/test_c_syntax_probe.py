"""Tests for CSyntaxProbe with mocked compiler calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.c_probe import CSyntaxProbe
from aura.syntax_probe.compiler_diagnostics import (
    _compiler_diag_re_for_target,
    _is_syntax_diagnostic,
    _is_unrelated_diagnostic,
    _parse_compiler_diagnostic,
)

_C_VALID = """\
#include <stdio.h>

int main(void) {
    printf("hello\\n");
    return 0;
}
"""

_C_INVALID = """\
#include <stdio.h>

int main(void) {
    printf("hello\\n"
    return 0;
}
"""


class TestCSyntaxProbe:
    """Tests for CSyntaxProbe."""

    def _probe(self) -> CSyntaxProbe:
        return CSyntaxProbe()

    # --- detect ---

    def test_detect_c_file(self) -> None:
        assert CSyntaxProbe.detect("main.c") is True
        assert CSyntaxProbe.detect("src/util.c") is True

    def test_detect_non_c_file(self) -> None:
        assert CSyntaxProbe.detect("main.cpp") is False
        assert CSyntaxProbe.detect("main.h") is False
        assert CSyntaxProbe.detect("main.py") is False
        assert CSyntaxProbe.detect("main.rs") is False

    def test_detect_does_not_claim_dot_h(self) -> None:
        assert CSyntaxProbe.detect("header.h") is False

    # --- valid C -> pass ---

    def test_valid_c_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "valid.c"
        c_file.write_text(_C_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.c")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "invalid.c"
        c_file.write_text(_C_INVALID)
        stderr_msg = "{path}:5:3: error: expected ';' before 'return'".format(
            path=str(c_file),
        )
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "invalid.c")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 5
        assert result.column == 3
        assert "expected" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    def test_syntax_error_columnless_format(self, tmp_path) -> None:
        """GCC sometimes omits the column; the probe should use column=1."""
        probe = self._probe()
        c_file = tmp_path / "nocol.c"
        c_file.write_text(_C_INVALID)
        stderr_msg = (
            "{path}:10: error: expected declaration specifiers".format(
                path=str(c_file),
            )
        )
        with patch("shutil.which", return_value="/usr/bin/gcc"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "nocol.c")
        assert result.evidence == "fail"
        assert result.line == 10
        assert result.column == 1
        assert result.toolchain_available is True

    # --- missing compiler -> no_evidence ---

    def test_missing_compiler_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "valid.c"
        c_file.write_text(_C_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.c")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- include error -> no_evidence ---

    def test_include_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "missing_include.c"
        c_file.write_text('#include <nonexistent.h>\nint main(void) { return 0; }\n')
        stderr_msg = (
            "{path}:1:1: fatal error: nonexistent.h: No such file or directory\n"
            "compilation terminated.\n"
        ).format(path=str(c_file))
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "missing_include.c")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- linker / undefined reference -> no_evidence ---

    def test_linker_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "link.c"
        c_file.write_text(_C_VALID)
        stderr_msg = (
            "undefined reference to 'main'\n"
            "collect2: error: ld returned 1 exit status\n"
        )
        with patch("shutil.which", return_value="/usr/bin/gcc"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "link.c")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- ambiguous nonzero output -> no_evidence ---

    def test_ambiguous_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "valid.c"
        c_file.write_text(_C_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "clang: error: no such file or directory: 'nonexistent.c'\n"
            )
            result = probe.check(tmp_path, "valid.c")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_c"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.c"
        outside_file.write_text(_C_VALID)
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
        result = probe.check(tmp_path, "../outside_workspace.c")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.c")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "slow.c"
        c_file.write_text(_C_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch(
                 "subprocess.run",
                 side_effect=subprocess.TimeoutExpired(
                     cmd="clang -fsyntax-only", timeout=30,
                 ),
             ):
            result = probe.check(tmp_path, "slow.c")
        assert result.evidence == "no_evidence"

    # --- OSError -> no_evidence ---

    def test_os_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        c_file = tmp_path / "broken.c"
        c_file.write_text(_C_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang"), \
             patch("subprocess.run", side_effect=OSError("enobufs")):
            result = probe.check(tmp_path, "broken.c")
        assert result.evidence == "no_evidence"


# --- _parse_compiler_diagnostic unit tests ---

class TestParseCompilerDiagnostic:
    """Direct tests for _parse_compiler_diagnostic."""

    def test_returns_none_for_empty_stderr(self) -> None:
        assert _parse_compiler_diagnostic("", "/tmp/test.c") is None

    def test_parses_standard_clang_format(self) -> None:
        stderr = "/tmp/test.c:5:3: error: expected ';' before 'return'\n"
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.c")
        assert result is not None
        line, col, msg = result
        assert line == 5
        assert col == 3
        assert "expected" in msg

    def test_parses_columnless_gcc_format(self) -> None:
        stderr = "/tmp/test.c:10: error: expected declaration specifiers\n"
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.c")
        assert result is not None
        line, col, msg = result
        assert line == 10
        assert col == 1
        assert "expected" in msg

    def test_ignores_fatal_include_error(self) -> None:
        stderr = (
            "/tmp/test.c:1:1: fatal error: stdio.h: No such file or directory\n"
        )
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.c")
        assert result is None

    def test_ignores_undefined_reference(self) -> None:
        stderr = "undefined reference to 'main'\n"
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.c")
        assert result is None

    def test_ignores_different_file_diagnostics(self) -> None:
        stderr = "/other/file.c:3:1: error: expected ';'\n"
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.c")
        assert result is None

    def test_parses_with_path_separator(self) -> None:
        stderr = "src/util.c:12:5: error: stray '\\377' in program\n"
        result = _parse_compiler_diagnostic(stderr, "src/util.c")
        assert result is not None
        line, col, msg = result
        assert line == 12
        assert col == 5
        assert "stray" in msg


# --- _is_syntax_diagnostic unit tests ---

class TestIsSyntaxDiagnostic:
    def test_expected_keyword(self) -> None:
        assert _is_syntax_diagnostic("expected ';' before 'return'") is True

    def test_parse_error_keyword(self) -> None:
        assert _is_syntax_diagnostic("parse error before 'int'") is True

    def test_stray_keyword(self) -> None:
        assert _is_syntax_diagnostic("stray '\\377' in program") is True

    def test_missing_terminating(self) -> None:
        assert _is_syntax_diagnostic("missing terminating '\"' character") is True

    def test_fatal_error_is_not_syntax(self) -> None:
        assert _is_syntax_diagnostic(
            "fatal error: stdio.h: No such file or directory"
        ) is False

    def test_undefined_reference_is_not_syntax(self) -> None:
        assert _is_syntax_diagnostic("undefined reference to 'printf'") is False

    def test_no_such_file_is_not_syntax(self) -> None:
        assert _is_syntax_diagnostic("no such file or directory") is False

    def test_required_from_is_not_syntax(self) -> None:
        assert _is_syntax_diagnostic(
            "required from 'template class Foo<int>'"
        ) is False

    def test_empty_message_returns_false(self) -> None:
        assert _is_syntax_diagnostic("") is False


# --- _is_unrelated_diagnostic unit tests ---

class TestIsUnrelatedDiagnostic:
    def test_fatal_error_is_unrelated(self) -> None:
        assert _is_unrelated_diagnostic(
            "fatal error: stdio.h: No such file or directory"
        ) is True

    def test_undefined_reference_is_unrelated(self) -> None:
        assert _is_unrelated_diagnostic(
            "undefined reference to 'main'"
        ) is True

    def test_no_such_file_is_unrelated(self) -> None:
        assert _is_unrelated_diagnostic("no such file or directory") is True

    def test_syntax_error_is_not_unrelated(self) -> None:
        assert _is_unrelated_diagnostic("expected ';' before 'return'") is False

    def test_empty_message_returns_false(self) -> None:
        assert _is_unrelated_diagnostic("") is False


# --- _compiler_diag_re_for_target unit tests ---

class TestCompilerDiagReForTarget:
    def test_matches_basic_diagnostic(self) -> None:
        pattern = _compiler_diag_re_for_target("/tmp/test.c")
        m = pattern.search("/tmp/test.c:5:3: error: expected ';'\n")
        assert m is not None
        assert m.group(1) == "5"
        assert m.group(2) == "3"
        assert "expected" in m.group(3)

    def test_matches_columnless_diagnostic(self) -> None:
        pattern = _compiler_diag_re_for_target("/tmp/test.c")
        m = pattern.search("/tmp/test.c:10: error: expected declaration\n")
        assert m is not None
        assert m.group(1) == "10"
        assert m.group(2) is None
