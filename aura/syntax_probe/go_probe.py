"""Syntax probe for Go using ``gofmt -d``."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_GO_TIMEOUT: int = 30

# gofmt error format: <file>:<line>:<col>: <message>
_GOFMT_ERROR_RE = re.compile(
    r"^" + re.escape(os.sep) + r"?[^:]+:(\d+):(\d+):\s*(.+)$",
    re.MULTILINE,
)


def _parse_gofmt_error(
    stderr: str, target_path: str,
) -> tuple[int, int, str] | None:
    """Try to extract (line, column, message) from gofmt stderr.

    gofmt errors look like::

        <file>:<line>:<col>: <message>

    Returns ``None`` when no error can be confidently parsed.
    """
    norm_target = os.path.normpath(target_path)
    for m in _GOFMT_ERROR_RE.finditer(stderr):
        line = int(m.group(1))
        col = int(m.group(2))
        message = m.group(3).strip()
        return line, col, message

    # Also try to find target file path in output line for line/col extraction.
    lines = stderr.splitlines()
    for line in lines:
        if norm_target in line:
            m = re.search(r":(\d+):(\d+):", line)
            if m:
                return int(m.group(1)), int(m.group(2)), line.strip()

    return None


class GoSyntaxProbe(SyntaxProbe):
    """Syntax probe for Go via ``gofmt -d``."""

    language_id: ClassVar[str] = "go"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".go")

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
        if shutil.which("gofmt") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run gofmt -d.
        try:
            proc = subprocess.run(
                ["gofmt", "-d", str(resolved)],
                capture_output=True,
                text=True,
                timeout=_GO_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        # Exit 0 — file is syntactically valid regardless of formatting diffs.
        if proc.returncode == 0:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Non-zero exit — try to extract a parse error.
        combined = proc.stderr or proc.stdout or ""
        parsed = _parse_gofmt_error(combined, path_str)
        if parsed is not None:
            line, column, message = parsed
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="fail",
                error=message,
                line=line,
                column=column,
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

register_probe(GoSyntaxProbe)
