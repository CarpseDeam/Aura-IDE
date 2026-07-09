from __future__ import annotations

import py_compile
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe


class PythonSyntaxProbe(SyntaxProbe):
    """Syntax probe for the Python language using stdlib ``py_compile``."""

    language_id: ClassVar[str] = "python"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".py")

    def check(
        self, workspace_root: str | Path, file_path: str | Path
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

        try:
            py_compile.compile(path_str, doraise=True)
        except py_compile.PyCompileError as e:
            original: BaseException | None = getattr(e, "exc_value", None)
            line: int | None = getattr(original, "lineno", None) if original else None
            column: int | None = getattr(original, "offset", None) if original else None
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="fail",
                error=str(e),
                line=line,
                column=column,
                failure_class="syntax_invalid",
            )
        except (ValueError, OSError, IOError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="pass",
        )


# Self-register on import
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(PythonSyntaxProbe)
