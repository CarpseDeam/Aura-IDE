"""Syntax probe for C# using ``dotnet build --no-restore`` with conservative diagnostic matching."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_CSHARP_TIMEOUT: int = 60

# Known C# syntax error codes.
_CSHARP_SYNTAX_CODES = re.compile(
    r"CS(?:1001|1003|1009|1010|1012|1013|1023|1024|1025|1026|1027|1028|"
    r"1031|1032|1035|1037|1038|1039|1040|1041|1042|1057|1058|1059|1065|"
    r"1513|1514|1518|1525|1526|1527|1528|1529|1530|1533|1534|1535|1536|"
    r"1537|1540|1541|1546|1547|1548|1549|1551|1552|1553|1555|1557|1558|"
    r"1559|1560|1597|1612|1615|1624|1625|1640|1646|1647|1648|1656|1658|"
    r"1659|1660|1661|1662|1663|1665|1670|1671|1672|1673|1674|1675|1676|"
    r"1677|1678|1679|1680|1681|1686|1689|1690|1691|1692|1717|1718|1719|"
    r"1720|1729|1730|1731|1732|1733|1734|1735|1736|1737|1738|1739|1740|"
    r"1741|1742|1743|1744|1745|1746|1747|1748|1749|1750|1751|1752|1753|"
    r"1754|1756|1757|1758|1759|1760|1761|1762|1763|1764|1765|1766|1767|"
    r"1768|1769|1770|1771|1772|1773|1774|1775|1776|1777|1778|1779|1780|"
    r"1781|1782|1783|1784|1785|1787|1788|1789|1790)\b",
)

# Restore, SDK, dependency, or semantic error patterns — NOT syntax.
_UNRELATED_CSHARP_CODES = re.compile(
    r"CS(?:0001|0002|0006|0007|0008|0009|0010|0011|0012|0013|0014|0015|"
    r"0016|0017|0018|0019|0020|0021|0022|0023|0024|0025|0026|0027|0028|"
    r"0029|0030|0031|0507|0518|0534|0535|0579|0617|0618|0619|0620|0621|"
    r"0622|0623|0625|0626|0628|0629|0631|0633|0635|0636|0637|0649|0659|"
    r"0675|0676|0677|0680|0681|0684|0685|0689|0690|0691|0692|0693|0695|"
    r"0702|0703|0704|0706|0708|0709|0718|0720|0721|0722|0723|0724|0726|"
    r"0727|0728|0729|0730|0731|0732|0736|0737|0738|0739|0740|0742|0743|"
    r"0744|0745|0746|0747|0748|0749|0750|0751|0753|0754|0755|0756|0757|"
    r"0758|0759|0760|0761|0762|0763|0764|0765|0766|0767|0768|0770|0771|"
    r"0773|0774|0775)\b",
)


def _is_syntax_error(line: str, target_path: str) -> bool:
    """Return True if the line is a known syntax diagnostic for *target_path*."""
    norm_target = os.path.normpath(target_path)
    if norm_target not in line:
        return False
    if _UNRELATED_CSHARP_CODES.search(line):
        return False
    if _CSHARP_SYNTAX_CODES.search(line):
        return True
    # Also check for generic "error CS" with syntax-like patterns.
    # Conservative — only match if it's a clear syntax error.
    return False


def _csharp_diag_re_for_target(target_path: str) -> re.Pattern:
    """Build a compiled regex to find C# diagnostics for *target_path*.

    Handles both ``/`` and ``\\`` path separators for cross-platform matching.
    """
    normed = os.path.normpath(target_path)
    escaped = re.escape(normed)
    escaped = escaped.replace("\\\\", "[\\\\/]")
    return re.compile(
        escaped + r"\((\d+),(\d+)\)\s*:\s*error\s+(CS\d+)\s*:\s*(.+)",
        re.MULTILINE,
    )


def _parse_csharp_diagnostic(
    stderr: str, target_path: str,
) -> tuple[int, int, str] | None:
    """Try to extract (line, column, message) from dotnet build output.

    dotnet build diagnostic format::

        <file>(<line>,<col>): error CS<code>: <message>

    Returns ``None`` when no syntax diagnostic can be confidently mapped.
    """
    pattern = _csharp_diag_re_for_target(target_path)

    for m in pattern.finditer(stderr):
        line = int(m.group(1))
        col = int(m.group(2))
        code = m.group(3)
        message = m.group(4).strip()

        # Only count syntax codes.
        if _CSHARP_SYNTAX_CODES.match(code):
            return line, col, f"{code}: {message}"
        # Skip unrelated codes.
        if _UNRELATED_CSHARP_CODES.match(code):
            continue

    return None


class CSharpSyntaxProbe(SyntaxProbe):
    """Syntax probe for C# via ``dotnet build --no-restore``."""

    language_id: ClassVar[str] = "csharp"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".cs")

    def _find_project_root(
        self, workspace_root: Path, file_path: Path,
    ) -> Path | None:
        """Walk upward from *file_path* to find a .csproj or .sln within workspace."""
        resolved = (
            file_path
            if file_path.is_absolute()
            else (workspace_root / file_path).resolve()
        )
        # Walk parents only — skip the file itself.
        for parent in [resolved.parent] + list(resolved.parents):
            if not str(parent).startswith(str(workspace_root)):
                return None
            # Check for .csproj or .sln
            csproj_files = list(parent.glob("*.csproj"))
            sln_files = list(parent.glob("*.sln"))
            if csproj_files or sln_files:
                return parent
        return None

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
        if shutil.which("dotnet") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Find project/solution root.
        project_root = self._find_project_root(workspace_root_path, resolved)
        if project_root is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=True,
            )

        # Run dotnet build --no-restore from project root.
        try:
            proc = subprocess.run(
                ["dotnet", "build", "--no-restore"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=_CSHARP_TIMEOUT,
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

        # Non-zero exit — look for syntax diagnostics on the target file.
        combined = proc.stderr or proc.stdout or ""
        parsed = _parse_csharp_diagnostic(combined, path_str)
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

        # Unrelated errors (restore, SDK, dependency) — no_evidence.
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="no_evidence",
            toolchain_available=True,
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(CSharpSyntaxProbe)
