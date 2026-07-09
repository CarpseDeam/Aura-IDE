from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe


class JSONSyntaxProbe(SyntaxProbe):
    """Syntax probe for JSON files using stdlib ``json.loads``."""

    language_id: ClassVar[str] = "json"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".json")

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
            text = resolved.read_text(encoding="utf-8")
            json.loads(text)
        except json.JSONDecodeError as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="fail",
                error=str(e),
                line=e.lineno,
                column=e.colno,
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

register_probe(JSONSyntaxProbe)
