"""Syntax probe for Shell scripts using ``bash -n``."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_BASH_TIMEOUT: int = 30

# bash -n error format: <path>: line <line>: <message>
_BASH_ERROR_RE = re.compile(
    r":\s*line\s+(\d+):\s+(.+)$",
    re.MULTILINE,
)


def _parse_bash_error(stderr: str, target_path: str) -> tuple[int, str] | None:
    """Extract (line, message) from bash stderr or return None.

    Bash syntax errors typically look like::

        /path/to/file.sh: line 5: syntax error: unexpected end of file
    """
    norm_target = target_path.replace("\\", "/").lower()
    for line in stderr.splitlines():
        line_normalized = line.replace("\\", "/").lower()
        if norm_target in line_normalized:
            m = _BASH_ERROR_RE.search(line)
            if m:
                return int(m.group(1)), m.group(2).strip()

    # Fallback: try regex anywhere in stderr.
    for m in _BASH_ERROR_RE.finditer(stderr):
        return int(m.group(1)), m.group(2).strip()

    return None


class ShellSyntaxProbe(SyntaxProbe):
    """Syntax probe for Shell scripts via ``bash -n``."""

    language_id: ClassVar[str] = "shell"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        path = str(file_path)
        return path.endswith(".sh") or path.endswith(".bash")

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
        if shutil.which("bash") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run bash -n.
        try:
            proc = subprocess.run(
                ["bash", "-n", str(resolved)],
                capture_output=True,
                text=True,
                timeout=_BASH_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        # Exit 0 — file is syntactically valid.
        if proc.returncode == 0:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Non-zero exit — try to extract a syntax error.
        parsed = _parse_bash_error(proc.stderr or "", path_str)
        if parsed is not None:
            line, message = parsed
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="fail",
                error=message,
                line=line,
                failure_class="syntax_invalid",
                toolchain_available=True,
            )

        # Ambiguous error that can't be mapped to this file's syntax.
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="no_evidence",
            toolchain_available=True,
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(ShellSyntaxProbe)
