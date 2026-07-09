from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from aura.syntax_probe.models import SyntaxProbeResult


class SyntaxProbe(ABC):
    """Abstract base for a syntax probe.

    Each subclass declares a ``language_id`` class variable and implements
    ``detect`` (file-suffix check) and ``check`` (file-on-disk analysis).
    """

    language_id: ClassVar[str]

    @staticmethod
    @abstractmethod
    def detect(file_path: str | Path) -> bool:
        """Return True if this probe claims the file based on its path."""
        ...

    @abstractmethod
    def check(
        self, workspace_root: str | Path, file_path: str | Path
    ) -> SyntaxProbeResult:
        """Run the probe against a real file on disk.

        Return ``SyntaxProbeResult(evidence="no_evidence")`` for a
        missing or unreadable file.
        """
        ...
