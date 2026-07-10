"""Tests for PowerShellSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import shutil
import subprocess
from unittest.mock import patch

import pytest

from aura.syntax_probe.powershell_probe import PowerShellSyntaxProbe


class TestPowerShellSyntaxProbe:
    """Tests for PowerShellSyntaxProbe."""

    def _probe(self) -> PowerShellSyntaxProbe:
        return PowerShellSyntaxProbe()

    # --- detect ---

    def test_detect_ps1_file(self) -> None:
        assert PowerShellSyntaxProbe.detect("script.ps1") is True
        assert PowerShellSyntaxProbe.detect("src/build.ps1") is True

    def test_detect_psm1_file(self) -> None:
        assert PowerShellSyntaxProbe.detect("module.psm1") is True

    def test_detect_psd1_file(self) -> None:
        assert PowerShellSyntaxProbe.detect("manifest.psd1") is True

    def test_detect_is_case_insensitive(self) -> None:
        assert PowerShellSyntaxProbe.detect("Build.PS1") is True

    def test_detect_non_powershell_file(self) -> None:
        assert PowerShellSyntaxProbe.detect("script.py") is False
        assert PowerShellSyntaxProbe.detect("script.sh") is False
        assert PowerShellSyntaxProbe.detect("script.bash") is False

    # --- valid PowerShell -> pass ---

    def test_valid_powershell_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        ps1_file = tmp_path / "valid.ps1"
        ps1_file.write_text('Write-Host "hello"\n')
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.ps1")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- parse error -> fail ---

    def test_parse_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        ps1_file = tmp_path / "invalid.ps1"
        ps1_file.write_text('if ($true {\nWrite-Host "hello"\n}\n')
        stdout_json = (
            '[{"Line":5,"Column":3,"Message":"Missing closing brace"}]'
        )
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout_json
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "invalid.ps1")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 5
        assert result.column == 3
        assert "Missing closing brace" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    def test_single_error_object_returns_fail(self, tmp_path) -> None:
        """Tolerate hosts that serialize a one-item array as an object."""
        probe = self._probe()
        ps1_file = tmp_path / "invalid.ps1"
        ps1_file.write_text("if ($true {\n")
        stdout_json = '{"Line":1,"Column":11,"Message":"Missing closing brace"}'
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout_json
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "invalid.ps1")
        assert result.evidence == "fail"
        assert result.line == 1
        assert result.column == 11

    # --- missing pwsh -> no_evidence ---

    def test_missing_pwsh_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        ps1_file = tmp_path / "valid.ps1"
        ps1_file.write_text('Write-Host "hello"\n')
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.ps1")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    def test_falls_back_to_windows_powershell(self, tmp_path) -> None:
        probe = self._probe()
        ps1_file = tmp_path / "valid.ps1"
        ps1_file.write_text('Write-Host "hello"\n')

        def find_executable(name: str) -> str | None:
            return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" if name == "powershell" else None

        with patch("shutil.which", side_effect=find_executable), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.ps1")
        assert result.evidence == "pass"
        assert mock_run.call_args.args[0][0].endswith("powershell.exe")

    # --- runtime error -> no_evidence ---

    def test_runtime_error_returns_no_evidence(self, tmp_path) -> None:
        """Non-zero exit with stderr noise and no JSON on stdout."""
        probe = self._probe()
        ps1_file = tmp_path / "valid.ps1"
        ps1_file.write_text('Write-Host "hello"\n')
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "pwsh: command not found or policy error\n"
            )
            result = probe.check(tmp_path, "valid.ps1")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- non-parseable JSON -> no_evidence ---

    def test_non_json_stdout_returns_no_evidence(self, tmp_path) -> None:
        """pwsh returned something that isn't JSON."""
        probe = self._probe()
        ps1_file = tmp_path / "valid.ps1"
        ps1_file.write_text('Write-Host "hello"\n')
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json at all"
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.ps1")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_ps1"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.ps1"
        outside_file.write_text('Write-Host "hello"\n')
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
        result = probe.check(tmp_path, "../outside_workspace.ps1")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.ps1")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        ps1_file = tmp_path / "slow.ps1"
        ps1_file.write_text('Write-Host "hello"\n')
        with patch("shutil.which", return_value="/usr/bin/pwsh"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="pwsh -NoProfile -NonInteractive -Command", timeout=30,
             )):
            result = probe.check(tmp_path, "slow.ps1")
        assert result.evidence == "no_evidence"

    @pytest.mark.skipif(
        shutil.which("pwsh") is None and shutil.which("powershell") is None,
        reason="PowerShell is not installed",
    )
    def test_real_parser_distinguishes_valid_and_invalid_files(self, tmp_path) -> None:
        """Exercise the .NET parser contract that subprocess mocks cannot verify."""
        valid = tmp_path / "valid.ps1"
        invalid = tmp_path / "invalid.ps1"
        valid.write_text('Write-Host "hello"\n')
        invalid.write_text("if ($true {\n")

        valid_result = self._probe().check(tmp_path, valid.name)
        invalid_result = self._probe().check(tmp_path, invalid.name)

        assert valid_result.evidence == "pass"
        assert invalid_result.evidence == "fail"
        assert invalid_result.line == 1
        assert invalid_result.column is not None
