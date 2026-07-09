"""Tests for CSharpSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from aura.syntax_probe.csharp_probe import (
    CSharpSyntaxProbe,
    _parse_csharp_diagnostic,
)

_CS_VALID = """\
using System;

class Hello
{
    static void Main()
    {
        Console.WriteLine("hello");
    }
}
"""

_CS_INVALID = """\
using System;

class Hello
{
    static void Main()
    {
        Console.WriteLine("hello"
    }
}
"""


class TestCSharpSyntaxProbe:
    """Tests for CSharpSyntaxProbe."""

    def _probe(self) -> CSharpSyntaxProbe:
        return CSharpSyntaxProbe()

    # --- detect ---

    def test_detect_cs_file(self) -> None:
        assert CSharpSyntaxProbe.detect("Hello.cs") is True
        assert CSharpSyntaxProbe.detect("src/Program.cs") is True

    def test_detect_non_cs_file(self) -> None:
        assert CSharpSyntaxProbe.detect("main.py") is False
        assert CSharpSyntaxProbe.detect("main.go") is False
        assert CSharpSyntaxProbe.detect("main.java") is False

    # --- valid C# -> pass ---

    def test_valid_csharp_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        with patch("shutil.which", return_value="/usr/bin/dotnet"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- known C# syntax diagnostic -> fail ---

    def test_syntax_diagnostic_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_INVALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        stderr_msg = (
            "{file}(7,7): error CS1003: Syntax error, ';' expected\n"
        ).format(file=str(cs_file))
        with patch("shutil.which", return_value="/usr/bin/dotnet"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 7
        assert result.column == 7
        assert "CS1003" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    # --- missing dotnet -> no_evidence ---

    def test_missing_dotnet_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- no .csproj/.sln -> no_evidence ---

    def test_no_csproj_or_sln_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        # No .csproj or .sln in tmp_path
        with patch("shutil.which", return_value="/usr/bin/dotnet"):
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- unrelated diagnostic -> no_evidence ---

    def test_restore_failure_returns_no_evidence(self, tmp_path) -> None:
        """Restore/dependency errors should not be classified as syntax failures."""
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        stderr_msg = (
            "{file}(1,1): error NU1100: Unable to resolve dependency\n"
        ).format(file=str(cs_file))
        with patch("shutil.which", return_value="/usr/bin/dotnet"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "no_evidence"
        assert result.failed is False
        assert result.toolchain_available is True

    def test_unrelated_cs_code_returns_no_evidence(self, tmp_path) -> None:
        """Semantic errors like CS0534 should not be classified as syntax failures."""
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        stderr_msg = (
            "{file}(3,14): error CS0534: 'Hello' does not implement inherited abstract member\n"
        ).format(file=str(cs_file))
        with patch("shutil.which", return_value="/usr/bin/dotnet"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = stderr_msg
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "no_evidence"
        assert result.failed is False

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_cs"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "Outside.cs"
        outside_file.write_text(_CS_VALID)
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
        result = probe.check(tmp_path, "../outside_workspace.cs")
        assert result.evidence == "no_evidence"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.cs")
        assert result.evidence == "no_evidence"

    # --- timeout -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        cs_file = tmp_path / "Hello.cs"
        cs_file.write_text(_CS_VALID)
        (tmp_path / "Hello.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <OutputType>Exe</OutputType>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        )
        with patch("shutil.which", return_value="/usr/bin/dotnet"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="dotnet build --no-restore", timeout=60,
             )):
            result = probe.check(tmp_path, "Hello.cs")
        assert result.evidence == "no_evidence"

    # --- _parse_csharp_diagnostic unit tests ---

    def test_parse_csharp_diagnostic_returns_none_for_empty(self) -> None:
        assert _parse_csharp_diagnostic("", "/tmp/Hello.cs") is None

    def test_parse_csharp_diagnostic_parses_syntax_code(self) -> None:
        result = _parse_csharp_diagnostic(
            "/tmp/Hello.cs(7,7): error CS1003: Syntax error, ';' expected\n",
            "/tmp/Hello.cs",
        )
        assert result is not None
        line, col, msg = result
        assert line == 7
        assert col == 7
        assert "CS1003" in msg

    def test_parse_csharp_diagnostic_ignores_unrelated_code(self) -> None:
        result = _parse_csharp_diagnostic(
            "/tmp/Hello.cs(1,1): error NU1100: Unable to resolve dependency\n",
            "/tmp/Hello.cs",
        )
        assert result is None
