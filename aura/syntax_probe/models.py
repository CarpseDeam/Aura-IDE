from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SyntaxProbeResult:
    """Result from running a single syntax probe against a file.

    ``ok`` is a convenience property — True only when *evidence* is ``"pass"``.
    ``failed`` is True only when *evidence* is ``"fail"``.
    ``has_evidence`` is True when *evidence* is not ``"no_evidence"``.
    """

    path: str
    language_id: str
    evidence: Literal["pass", "fail", "no_evidence"]
    error: str = ""
    line: int | None = None
    column: int | None = None
    toolchain_available: bool | None = None
    command: str = ""
    raw_output: str = ""
    failure_class: str = ""

    @property
    def ok(self) -> bool:
        """True only when the probe found no issues."""
        return self.evidence == "pass"

    @property
    def failed(self) -> bool:
        """True only when the probe found a definitive failure."""
        return self.evidence == "fail"

    @property
    def has_evidence(self) -> bool:
        """True when evidence is not ``no_evidence``."""
        return self.evidence != "no_evidence"
