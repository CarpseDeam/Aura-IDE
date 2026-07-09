"""Tests for ShellSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.shell_probe import ShellSyntaxProbe, _parse_bash_error


class TestShellSyntaxProbe:
    """Tests for ShellSyntaxProbe."""

    def _probe(self) -> ShellSyntaxProbe:
        return ShellSyntaxProbe()

    # --- detect ---

    def test_detect_sh_file(self) -> None:
        assert ShellSyntaxProbe.detect("script.sh") is True
        assert ShellSyntaxProbe.detect("src/build.sh") is True

    def test_detect_bash_file(self) -> None:
        assert ShellSyntaxProbe.detect("script.bash") is True
        assert ShellSyntaxProbe.detect("lib/compat.bash") is True

    def test_detect_non_shell_file(self) -> None:
        assert ShellSyntaxProbe.detect("script.py") is False
        assert ShellSyntaxProbe.detect("script.md") is False
        assert ShellSyntaxProbe.detect("script.zsh") is False
        assert ShellSyntaxProbe.detect("script.ps1") is False

    # --- valid shell -> pass ---

    def test_valid_shell_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        sh_file = tmp_path / "valid.sh"
        sh_file.write_text("#!/bin/bash\necho hello\n")
        with patch("shutil.which", return_value="/usr/bin/bash"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.sh")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        sh_file = tmp_path / "invalid.sh"
        sh_file.write_text("#!/bin/bash\nif true\necho hello\nfi\n")
        stderr_msg = f"{sh_file}: line 2: syntax error: unexpected end of file"
        with patch("shutil.which", return_value="/usr/bin/bash"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg + "\n"
            result = probe.check(tmp_path, "invalid.sh")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 2
        assert "syntax error" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    # --- missing bash -> no_evidence ---

    def test_missing_bash_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        sh_file = tmp_path / "valid.sh"
        sh_file.write_text("#!/bin/bash\necho hello\n")
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.sh")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- ambiguous stderr -> no_evidence ---

    def test_ambiguous_output_returns_no_evidence(self, tmp_path) -> None:
        """Non-syntax bash errors should yield no_evidence."""
        probe = self._probe()
        sh_file = tmp_path / "valid.sh"
        sh_file.write_text("#!/bin/bash\necho hello\n")
        with patch("shutil.which", return_value="/usr/bin/bash"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 2
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "bash: /nonexistent/file.sh: No such file or directory\n"
            )
            result = probe.check(tmp_path, "valid.sh")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_sh"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.sh"
        outside_file.write_text("#!/bin/bash\necho hello\n")
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
        result = probe.check(tmp_path, "../outside_workspace.sh")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.sh")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        sh_file = tmp_path / "slow.sh"
        sh_file.write_text("#!/bin/bash\necho hello\n")
        with patch("shutil.which", return_value="/usr/bin/bash"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="bash -n", timeout=30,
             )):
            result = probe.check(tmp_path, "slow.sh")
        assert result.evidence == "no_evidence"

    # --- _parse_bash_error unit tests ---

    def test_parse_bash_error_returns_none_for_empty_output(self) -> None:
        assert _parse_bash_error("", "/tmp/test.sh") is None

    def test_parse_bash_error_parses_standard_format(self) -> None:
        result = _parse_bash_error(
            "/tmp/test.sh: line 5: syntax error: unexpected end of file\n",
            "/tmp/test.sh",
        )
        assert result is not None
        line, msg = result
        assert line == 5
        assert "syntax error" in msg
