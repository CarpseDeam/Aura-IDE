"""Syntax probe for JavaScript using ``node --check``."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_NODE_TIMEOUT: int = 30


def _parse_node_error(
    stderr: str, target_path: str,
) -> tuple[int, int, str] | None:
    """Try to extract (line, column, message) from *stderr*.

    Node error output formats::

        SyntaxError: Unexpected token (2:5)
        SyntaxError: Unexpected identifier\\n    at file.js:3:10

    Returns ``None`` when the error cannot be confidently mapped to the
    target file.
    """
    norm_target = os.path.normpath(target_path)

    # Pattern: SyntaxError: <message> (line:col)
    m = re.search(r"\((\d+):(\d+)\)\s*$", stderr)
    if m:
        return int(m.group(1)), int(m.group(2)), stderr.strip()

    # Pattern: SyntaxError: <message> at <file>:line:col
    lines = stderr.splitlines()
    for line in lines:
        m = re.search(
            r"at\s+" + re.escape(norm_target) + r":(\d+):(\d+)",
            line,
        )
        if m:
            return int(m.group(1)), int(m.group(2)), stderr.strip()

    # If the stderr mentions the target file path in any line, try
    # a fallback line/column extraction.
    if norm_target in stderr:
        # Try a generic "line:col" at end of any line.
        for line in lines:
            m = re.search(r":(\d+):(\d+)\s*$", line)
            if m:
                return int(m.group(1)), int(m.group(2)), stderr.strip()

    return None


class JavaScriptSyntaxProbe(SyntaxProbe):
    """Syntax probe for JavaScript via ``node --check``."""

    language_id: ClassVar[str] = "javascript"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        path_str = str(file_path)
        return path_str.endswith((".js", ".mjs", ".cjs"))

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
        if shutil.which("node") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run node --check.
        try:
            proc = subprocess.run(
                ["node", "--check", str(resolved)],
                capture_output=True,
                text=True,
                timeout=_NODE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        if proc.returncode == 0:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Non-zero exit — try to extract a parse error.
        combined = proc.stderr or proc.stdout or ""
        parsed = _parse_node_error(combined, path_str)
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

register_probe(JavaScriptSyntaxProbe)
