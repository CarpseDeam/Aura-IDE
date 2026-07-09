"""Tests for GoSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.go_probe import GoSyntaxProbe, _parse_gofmt_error

_GO_VALID = """\
package main

import "fmt"

func main() {
\tfmt.Println("hello")
}
"""

_GO_INVALID = """\
package main

import "fmt"

func main() {
\tfmt.Println("hello"
}
"""


class TestGoSyntaxProbe:
    """Tests for GoSyntaxProbe."""

    def _probe(self) -> GoSyntaxProbe:
        return GoSyntaxProbe()

    # --- detect ---

    def test_detect_go_file(self) -> None:
        assert GoSyntaxProbe.detect("main.go") is True
        assert GoSyntaxProbe.detect("src/lib.go") is True

    def test_detect_non_go_file(self) -> None:
        assert GoSyntaxProbe.detect("main.py") is False
        assert GoSyntaxProbe.detect("main.rs") is False
        assert GoSyntaxProbe.detect("main.java") is False

    # --- valid Go -> pass ---

    def test_valid_go_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        go_file = tmp_path / "valid.go"
        go_file.write_text(_GO_VALID)
        with patch("shutil.which", return_value="/usr/bin/gofmt"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.go")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    def test_valid_go_with_formatting_diff_still_passes(self, tmp_path) -> None:
        """gofmt may produce formatting diffs on stdout but exit 0 — syntax is valid."""
        probe = self._probe()
        go_file = tmp_path / "unformatted.go"
        go_file.write_text(_GO_VALID)
        with patch("shutil.which", return_value="/usr/bin/gofmt"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                "diff -u original formatted\n-func main() {\n+func main() {\n"
            )
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "unformatted.go")
        assert result.evidence == "pass"
        assert result.ok is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        go_file = tmp_path / "invalid.go"
        go_file.write_text(_GO_INVALID)
        stderr_msg = "{go}:5:23: expected ';', found '}}'".format(
            go=str(go_file),
        )
        with patch("shutil.which", return_value="/usr/bin/gofmt"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "invalid.go")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 5
        assert result.column == 23
        assert "expected" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    # --- missing gofmt -> no_evidence ---

    def test_missing_gofmt_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        go_file = tmp_path / "valid.go"
        go_file.write_text(_GO_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.go")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- ambiguous nonzero output -> no_evidence ---

    def test_ambiguous_error_returns_no_evidence(self, tmp_path) -> None:
        """Non-syntax gofmt errors should yield no_evidence."""
        probe = self._probe()
        go_file = tmp_path / "valid.go"
        go_file.write_text(_GO_VALID)
        with patch("shutil.which", return_value="/usr/bin/gofmt"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 2
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "gofmt: can't open file /nonexistent/foo.go\n"
            )
            result = probe.check(tmp_path, "valid.go")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_go"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.go"
        outside_file.write_text(_GO_VALID)
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
        result = probe.check(tmp_path, "../outside_workspace.go")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.go")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        go_file = tmp_path / "slow.go"
        go_file.write_text(_GO_VALID)
        with patch("shutil.which", return_value="/usr/bin/gofmt"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="gofmt -d", timeout=30,
             )):
            result = probe.check(tmp_path, "slow.go")
        assert result.evidence == "no_evidence"

    # --- _parse_gofmt_error unit tests ---

    def test_parse_gofmt_error_returns_none_for_empty_output(self) -> None:
        assert _parse_gofmt_error("", "/tmp/test.go") is None

    def test_parse_gofmt_error_parses_standard_format(self) -> None:
        result = _parse_gofmt_error(
            "/tmp/test.go:5:23: expected ';', found '}'\n",
            "/tmp/test.go",
        )
        assert result is not None
        line, col, msg = result
        assert line == 5
        assert col == 23
        assert "expected" in msg
