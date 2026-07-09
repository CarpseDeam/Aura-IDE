"""Syntax probe for GDScript using tree-sitter.

Delegates to ``_tree_sitter_check`` from ``aura.syntax_probe.tree_sitter_utils``
for pure syntax parsing — no Godot binary required.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe
from aura.syntax_probe.tree_sitter_utils import _tree_sitter_check


class GDScriptSyntaxProbe(SyntaxProbe):
    """Syntax probe for GDScript files via tree-sitter."""

    language_id: ClassVar[str] = "gdscript"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".gd")

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

        evidence, line, column, message = _tree_sitter_check(resolved, "gdscript")

        if evidence == "pass":
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
            )

        if evidence == "fail":
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="fail",
                error=message,
                line=line,
                column=column,
                failure_class="syntax_invalid",
            )

        # evidence == "no_evidence"
        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="no_evidence",
            error=message,
        )


# Self-register on import
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(GDScriptSyntaxProbe)
