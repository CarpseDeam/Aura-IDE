from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

# Regex to extract line/column from TOMLDecodeError messages like:
#   "Expected ']' at the end of a table declaration (at line 1, column 9)"
_TOML_LOC_RE = re.compile(r"\(at line (\d+), column (\d+)\)")


def _extract_toml_location(error_message: str) -> tuple[int | None, int | None]:
    """Extract line and column from a TOMLDecodeError message, if present."""
    m = _TOML_LOC_RE.search(error_message)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


class TOMLSyntaxProbe(SyntaxProbe):
    """Syntax probe for TOML files using stdlib ``tomllib``."""

    language_id: ClassVar[str] = "toml"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".toml")

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
            import tomllib  # Python 3.11+; this project runs 3.13

            text = resolved.read_text(encoding="utf-8")
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:  # noqa: F821
            line, column = _extract_toml_location(str(e))
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

register_probe(TOMLSyntaxProbe)
