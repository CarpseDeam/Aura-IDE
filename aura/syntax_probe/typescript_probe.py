"""Syntax probe for TypeScript using Node + TypeScript compiler API."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe

_TS_TIMEOUT: int = 30

_INLINE_SCRIPT = r"""
const path = require('path');
const fs = require('fs');

const filePath = process.argv[1];
let ts;
try {
  ts = require(require.resolve('typescript'));
} catch (e) {
  process.stderr.write('typescript: ' + (e.message || String(e)));
  process.exit(1);
}

const sourceText = fs.readFileSync(filePath, 'utf-8');
const sourceFile = ts.createSourceFile(
  path.basename(filePath),
  sourceText,
  ts.ScriptTarget.Latest,
  true
);

const diagnostics = sourceFile.parseDiagnostics;
const results = [];
for (const d of diagnostics) {
  if (d.file) {
    const start = d.file.getLineAndCharacterOfPosition(d.start);
    results.push({
      line: start.line + 1,
      column: start.character + 1,
      messageText: typeof d.messageText === 'string' ? d.messageText : d.messageText.messageText,
    });
  }
}
process.stdout.write(JSON.stringify({ diagnostics: results }));
process.exit(0);
"""


class TypeScriptSyntaxProbe(SyntaxProbe):
    """Syntax probe for TypeScript via Node + TypeScript compiler API."""

    language_id: ClassVar[str] = "typescript"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        path_str = str(file_path)
        return path_str.endswith((".ts", ".tsx", ".mts", ".cts"))

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

        # Run Node with inline script.
        try:
            proc = subprocess.run(
                ["node", "-e", _INLINE_SCRIPT, str(resolved)],
                capture_output=True,
                text=True,
                timeout=_TS_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        # Check if TypeScript module could not be resolved.
        if proc.returncode != 0:
            stderr_lower = (proc.stderr or "").lower()
            if "cannot find module" in stderr_lower or "typescript:" in stderr_lower:
                return SyntaxProbeResult(
                    path=path_str,
                    language_id=self.language_id,
                    evidence="no_evidence",
                    toolchain_available=True,
                    error=proc.stderr.strip(),
                )
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=proc.stderr.strip() or proc.stdout.strip(),
            )

        # Parse JSON output.
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        if not isinstance(data, dict):
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        diagnostics = data.get("diagnostics", [])
        if not isinstance(diagnostics, list) or len(diagnostics) == 0:
            # No parse diagnostics — clean syntax.
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="pass",
                toolchain_available=True,
            )

        # Use the first parse diagnostic for line/column.
        first = diagnostics[0]
        message = first.get("messageText", "")
        line = first.get("line")
        column = first.get("column")
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


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(TypeScriptSyntaxProbe)
