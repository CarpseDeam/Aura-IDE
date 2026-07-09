"""Tests for CppSyntaxProbe with mocked compiler calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.cpp_probe import CppSyntaxProbe
from aura.syntax_probe.compiler_diagnostics import _parse_compiler_diagnostic

_CPP_VALID = """\
#include <iostream>

int main() {
    std::cout << "hello" << std::endl;
    return 0;
}
"""

_CPP_INVALID = """\
#include <iostream>

int main() {
    std::cout << "hello" << std::endl
    return 0;
}
"""


class TestCppSyntaxProbe:
    """Tests for CppSyntaxProbe."""

    def _probe(self) -> CppSyntaxProbe:
        return CppSyntaxProbe()

    # --- detect ---

    def test_detect_cpp_extensions(self) -> None:
        assert CppSyntaxProbe.detect("main.cpp") is True
        assert CppSyntaxProbe.detect("main.cc") is True
        assert CppSyntaxProbe.detect("main.cxx") is True
        assert CppSyntaxProbe.detect("main.hpp") is True
        assert CppSyntaxProbe.detect("main.hh") is True
        assert CppSyntaxProbe.detect("main.hxx") is True

    def test_detect_rejects_c_file(self) -> None:
        assert CppSyntaxProbe.detect("main.c") is False

    def test_detect_rejects_dot_h(self) -> None:
        assert CppSyntaxProbe.detect("header.h") is False

    def test_detect_rejects_unrelated(self) -> None:
        assert CppSyntaxProbe.detect("main.py") is False
        assert CppSyntaxProbe.detect("main.rs") is False
        assert CppSyntaxProbe.detect("main.java") is False

    # --- valid C++ -> pass ---

    def test_valid_cpp_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "valid.cpp"
        cpp_file.write_text(_CPP_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.cpp")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "invalid.cpp"
        cpp_file.write_text(_CPP_INVALID)
        stderr_msg = "{path}:5:3: error: expected ';' at end of member declaration".format(
            path=str(cpp_file),
        )
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "invalid.cpp")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 5
        assert result.column == 3
        assert "expected" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    def test_syntax_error_columnless_format(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "nocol.cpp"
        cpp_file.write_text(_CPP_INVALID)
        stderr_msg = "{path}:10: error: expected ',' or '...' before 'return'".format(
            path=str(cpp_file),
        )
        with patch("shutil.which", return_value="/usr/bin/g++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "nocol.cpp")
        assert result.evidence == "fail"
        assert result.line == 10
        assert result.column == 1
        assert result.toolchain_available is True

    # --- template context note is ignored ---

    def test_template_note_is_ignored(self, tmp_path) -> None:
        """Template instantiation notes are not syntax errors."""
        probe = self._probe()
        cpp_file = tmp_path / "template_test.cpp"
        cpp_file.write_text(_CPP_INVALID)
        stderr_msg = (
            "{path}:5:3: error: expected ';' at end of member declaration\n"
            "{path}:3:7: note: in instantiation of template class 'Foo<int>'\n"
        ).format(path=str(cpp_file))
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "template_test.cpp")
        # Still fails on the syntax error (line 5), not the note (line 3).
        assert result.evidence == "fail"
        assert result.line == 5
        assert result.column == 3
        assert result.toolchain_available is True

    # --- missing compiler -> no_evidence ---

    def test_missing_compiler_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "valid.cpp"
        cpp_file.write_text(_CPP_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.cpp")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- include error -> no_evidence ---

    def test_include_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "missing_include.cpp"
        cpp_file.write_text(
            '#include <nonexistent.hpp>\nint main() { return 0; }\n',
        )
        stderr_msg = (
            "{path}:1:1: fatal error: nonexistent.hpp: No such file or directory\n"
            "compilation terminated.\n"
        ).format(path=str(cpp_file))
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "missing_include.cpp")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- linker / undefined reference -> no_evidence ---

    def test_linker_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "link.cpp"
        cpp_file.write_text(_CPP_VALID)
        stderr_msg = (
            "undefined reference to 'main'\n"
            "collect2: error: ld returned 1 exit status\n"
        )
        with patch("shutil.which", return_value="/usr/bin/g++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "link.cpp")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- ambiguous nonzero output -> no_evidence ---

    def test_ambiguous_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "valid.cpp"
        cpp_file.write_text(_CPP_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "clang++: error: no such file or directory: 'nonexistent.cpp'\n"
            )
            result = probe.check(tmp_path, "valid.cpp")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_cpp"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.cpp"
        outside_file.write_text(_CPP_VALID)
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
        result = probe.check(tmp_path, "../outside_workspace.cpp")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.cpp")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "slow.cpp"
        cpp_file.write_text(_CPP_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch(
                 "subprocess.run",
                 side_effect=subprocess.TimeoutExpired(
                     cmd="clang++ -fsyntax-only", timeout=30,
                 ),
             ):
            result = probe.check(tmp_path, "slow.cpp")
        assert result.evidence == "no_evidence"

    # --- OSError -> no_evidence ---

    def test_os_error_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cpp_file = tmp_path / "broken.cpp"
        cpp_file.write_text(_CPP_VALID)
        with patch("shutil.which", return_value="/usr/bin/clang++"), \
             patch("subprocess.run", side_effect=OSError("enobufs")):
            result = probe.check(tmp_path, "broken.cpp")
        assert result.evidence == "no_evidence"


# --- _parse_compiler_diagnostic C++-specific tests ---

class TestParseCompilerDiagnosticCpp:
    def test_template_context_note_is_ignored(self) -> None:
        stderr = (
            "/tmp/test.cpp:5:3: error: expected ';' at end of member declaration\n"
            "/tmp/test.cpp:3:7: note: in instantiation of template class 'Foo<int>'\n"
        )
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.cpp")
        assert result is not None
        line, col, msg = result
        # The syntax error is on line 5, col 3 — not the note on line 3.
        assert line == 5
        assert col == 3
        assert "expected" in msg

    def test_collect2_linker_error_ignored(self) -> None:
        stderr = (
            "/tmp/test.cpp:1:1: error: expected ';' before 'return'\n"
            "collect2: error: ld returned 1 exit status\n"
        )
        result = _parse_compiler_diagnostic(stderr, "/tmp/test.cpp")
        assert result is not None
        line, col, msg = result
        assert line == 1
        assert col == 1
