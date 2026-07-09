"""Syntax probe for Java using ``javac`` with conservative diagnostic filtering."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_JAVA_TIMEOUT: int = 60

# Regex to match javac error diagnostic mapped to a specific target file.
# Format: <file>:<line>: error: <message>
# or:     <file>:<line>:<column>: error: <message>
# The regex is constructed dynamically with the escaped target path.
def _javac_error_re_for_target(target_path: str) -> re.Pattern:
    """Build a compiled regex to find javac errors for *target_path*.

    Handles both ``/`` and ``\\`` path separators so it works on any
    platform regardless of how javac quotes paths.
    """
    normed = os.path.normpath(target_path)
    # Escape for regex, then make path separators accept either \ or /
    escaped = re.escape(normed)
    # Replace the escaped backslash (which is \\\\ in string literal)
    # with a char class matching \ or /
    escaped = escaped.replace("\\\\", "[\\\\/]")
    return re.compile(
        escaped + r":(\d+)(?::(\d+))?:\s*error:\s*(.+)$",
        re.MULTILINE,
    )

# Patterns for diagnostics that are NOT syntax errors — dependency,
# classpath, symbol resolution, etc.
_UNRELATED_DIAGNOSTICS = re.compile(
    r"cannot find symbol"
    r"|package\s+\S+\s+does not exist"
    r"|cannot access"
    r"|classpath"
    r"|is not abstract and does not override"
    r"|unchecked warning"
    r"|unchecked cast"
    r"|uses unchecked or unsafe operations",
    re.IGNORECASE,
)


def _is_syntax_error_line(line: str, target_path: str) -> bool:
    """Return True if *line* is a javac error diagnostic mapped to *target_path*
    and does NOT appear to be a dependency/symbol/classpath issue."""
    norm_target = os.path.normpath(target_path)
    if norm_target not in line:
        return False
    if "error:" not in line:
        return False
    if _UNRELATED_DIAGNOSTICS.search(line):
        return False
    return True


def _parse_javac_error(
    stderr: str, target_path: str,
) -> tuple[int, int, str] | None:
    """Try to extract (line, column, message) from javac stderr.

    Javac output formats::

        <file>:<line>: error: <message>
        <file>:<line>:<column>: error: <message>

    Returns ``None`` when no syntax error can be confidently mapped to the
    target file.
    """
    re_pattern = _javac_error_re_for_target(target_path)

    for m in re_pattern.finditer(stderr):
        line = int(m.group(1))
        col = int(m.group(2)) if m.group(2) else 1
        message = m.group(3).strip()

        # Skip dependency/symbol/classpath diagnostics.
        if _UNRELATED_DIAGNOSTICS.search(message):
            continue

        return line, col, message

    return None


class JavaSyntaxProbe(SyntaxProbe):
    """Syntax probe for Java via ``javac`` with conservative filtering."""

    language_id: ClassVar[str] = "java"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".java")

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
        if shutil.which("javac") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run javac into a temp directory to minimize classpath pollution.
        tmpdir: str | None = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="aura_javac_")
            proc = subprocess.run(
                ["javac", "-d", tmpdir, str(resolved)],
                capture_output=True,
                text=True,
                timeout=_JAVA_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )
        finally:
            if tmpdir is not None:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except OSError:
                    pass

        if proc.returncode == 0:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Non-zero exit — try to extract a syntax error mapped to this file.
        combined = proc.stderr or proc.stdout or ""
        parsed = _parse_javac_error(combined, path_str)
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

        # Unrelated errors (symbols, classpath, etc.) — no_evidence.
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="no_evidence",
            toolchain_available=True,
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(JavaSyntaxProbe)
