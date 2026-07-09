"""Syntax probe for the Rust language using ``cargo check --message-format=json``."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.protocol import SyntaxProbe


class RustSyntaxProbe(SyntaxProbe):
    """Syntax probe for Rust files via cargo check JSON output."""

    language_id: ClassVar[str] = "rust"

    @staticmethod
    def detect(file_path: str | Path) -> bool:
        return str(file_path).endswith(".rs")

    def _find_crate_root(
        self, workspace_root: Path, file_path: Path
    ) -> Path | None:
        """Walk upward from *file_path* to find a Cargo.toml within workspace."""
        resolved = (
            file_path
            if file_path.is_absolute()
            else (workspace_root / file_path).resolve()
        )
        for parent in [resolved] + list(resolved.parents):
            if not str(parent).startswith(str(workspace_root)):
                return None
            if (parent / "Cargo.toml").is_file():
                return parent
        return None

    def check(
        self, workspace_root: str | Path, file_path: str | Path
    ) -> SyntaxProbeResult:
        workspace_root_path = Path(workspace_root).resolve(strict=False)

        if Path(file_path).is_absolute():
            resolved = Path(file_path).resolve(strict=False)
        else:
            resolved = (workspace_root_path / file_path).resolve(strict=False)

        path_str = str(resolved)

        # Verify within workspace boundary.
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

        # Find crate root — walk upward from file to find Cargo.toml
        crate_root = self._find_crate_root(workspace_root_path, resolved)
        if crate_root is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        # Check toolchain availability.
        if shutil.which("cargo") is None:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                toolchain_available=False,
            )

        # Run cargo check with JSON output.
        try:
            proc = subprocess.run(
                ["cargo", "check", "--message-format=json"],
                cwd=str(crate_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
                error=str(e),
            )

        return self._parse_cargo_output(proc, path_str)

    def _parse_cargo_output(
        self, proc: subprocess.CompletedProcess, path_str: str
    ) -> SyntaxProbeResult:
        """Parse ``cargo check --message-format=json`` output.

        Returns ``fail`` only when a compiler-message with level ``error``
        has a primary span mapping to *path_str*.  If errors exist but
        none target the file, returns ``no_evidence``.  If no errors at
        all, returns ``pass``.
        """
        target_path = os.path.normpath(path_str)
        had_any_error = False

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("reason") != "compiler-message":
                continue

            message = msg.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("level") != "error":
                continue

            had_any_error = True

            spans = message.get("spans")
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                if span.get("is_primary") is not True:
                    continue
                span_path = span.get("file_name", "")
                if os.path.normpath(span_path) == target_path:
                    return SyntaxProbeResult(
                        path=path_str,
                        language_id=self.language_id,
                        evidence="fail",
                        error=message.get(
                            "rendered", message.get("message", "")
                        ),
                        line=span.get("line_start"),
                        column=span.get("column_start"),
                        failure_class="syntax_invalid",
                    )

        # No primary error span for this file found.
        if had_any_error:
            return SyntaxProbeResult(
                path=path_str,
                language_id=self.language_id,
                evidence="no_evidence",
            )

        return SyntaxProbeResult(
            path=path_str,
            language_id=self.language_id,
            evidence="pass",
        )


# Self-register on import.
from aura.syntax_probe.registry import register_probe  # noqa: E402

register_probe(RustSyntaxProbe)
