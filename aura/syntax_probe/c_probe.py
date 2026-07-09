"""Syntax probe for C using ``clang -fsyntax-only`` / ``gcc -fsyntax-only``."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.compiler_diagnostics import _parse_compiler_diagnostic
from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_C_TIMEOUT: int = 30


class CSyntaxProbe(SyntaxProbe):
    """Syntax probe for C via compiler ``-fsyntax-only``."""

    language_id: ClassVar[str] = "c"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".c")

    def check(
        self,
        workspace_root: str | Path,
        file_path: str | Path,
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

        # Check toolchain availability — prefer clang, fall back to gcc.
        compiler: str | None = shutil.which("clang") or shutil.which("gcc")
        if compiler is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run the compiler with -fsyntax-only.
        try:
            proc = subprocess.run(
                [compiler, "-fsyntax-only", str(resolved)],
                capture_output=True,
                text=True,
                timeout=_C_TIMEOUT,
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

        # Non-zero exit — try to extract a syntax diagnostic.
        combined = proc.stderr or proc.stdout or ""
        parsed = _parse_compiler_diagnostic(combined, path_str)
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

        # Ambiguous / non-syntax error (include, linker, etc.).
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="no_evidence",
            toolchain_available=True,
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(CSyntaxProbe)
