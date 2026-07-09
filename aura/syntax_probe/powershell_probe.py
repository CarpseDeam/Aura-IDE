"""Syntax probe for PowerShell scripts using the .NET parser API via pwsh."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_PWSH_TIMEOUT: int = 30

# Inline PowerShell command that parses a file and outputs JSON error array.
# Use {{ and }} to escape literal braces for Python str.format().
_PWSH_PARSE_COMMAND = (
    "$errors = [System.Management.Automation.Language.Parser]::ParseFile("
    "'{file}', [ref]$null, [ref]$null).Errors; "
    "if ($errors) {{ "
    "$errors | ForEach-Object {{ "
    "[PSCustomObject]@{{"
    "Line=$_.Token.Extent.StartLineNumber;"
    "Column=$_.Token.Extent.StartColumnNumber;"
    "Message=$_.Message"
    "}} "
    "}} | ConvertTo-Json -Compress "
    "}} else {{ '[]' }}"
)


class PowerShellSyntaxProbe(SyntaxProbe):
    """Syntax probe for PowerShell via the .NET parser API."""

    language_id: ClassVar[str] = "powershell"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        path = str(file_path)
        return path.endswith(".ps1") or path.endswith(".psm1") or path.endswith(".psd1")

    def check(
        self, workspace_root: str | Path, file_path: str | Path,
    ) -> SyntaxProbeResult:
        workspace_root_path = Path(workspace_root).resolve(strict=False)

        if Path(file_path).is_absolute():
            resolved = Path(file_path).resolve(strict=False)
        else:
            resolved = (workspace_root_path / file_path).resolve(strict=False)

        path_str = str(resolved)

        # Verify resolved path is within the workspace boundary.
        try:
            resolved.relative_to(workspace_root_path)
        except ValueError:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        if not resolved.is_file():
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        # Check toolchain availability.
        if shutil.which("pwsh") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Build the inline PowerShell command with the file path.
        # Escape single quotes in the path for the PowerShell string.
        escaped_path = str(resolved).replace("'", "''")
        ps_command = _PWSH_PARSE_COMMAND.format(file=escaped_path)

        # Run pwsh with the inline parser command.
        try:
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps_command],
                capture_output=True,
                text=True,
                timeout=_PWSH_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        # If pwsh itself failed to launch or crashed, treat as no_evidence.
        if proc.returncode != 0 and not proc.stdout.strip():
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=True,
            )

        # Parse stdout JSON.
        stdout = proc.stdout.strip()
        if not stdout:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=True,
            )

        try:
            errors: list[dict[str, Any]] = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=True,
            )

        # Empty array — no errors.
        if not errors:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Report the first error.
        first = errors[0]
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="fail",
            error=first.get("Message", "Syntax error"),
            line=first.get("Line"),
            column=first.get("Column"),
            failure_class="syntax_invalid",
            toolchain_available=True,
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(PowerShellSyntaxProbe)
