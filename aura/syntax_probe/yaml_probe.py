from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


class YAMLSyntaxProbe(SyntaxProbe):
    """Syntax probe for YAML files using PyYAML ``yaml.safe_load``."""

    language_id: ClassVar[str] = "yaml"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        path_str = str(file_path)
        return path_str.endswith(".yaml") or path_str.endswith(".yml")

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

        if not _YAML_AVAILABLE:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        try:
            text = resolved.read_text(encoding="utf-8")
            yaml.safe_load(text)
        except yaml.YAMLError as e:  # noqa: F821
            line: int | None = None
            column: int | None = None
            if hasattr(e, "problem_mark") and e.problem_mark is not None:
                line = e.problem_mark.line + 1
                column = e.problem_mark.column + 1
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

register_probe(YAMLSyntaxProbe)
