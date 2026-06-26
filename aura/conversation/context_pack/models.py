"""Dataclasses for Worker Context Pack sections and results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextPackSection:
    """A single named section within a Worker Context Pack."""

    heading: str
    body_lines: list[str]
    caveat: str | None = None


@dataclass
class ContextPackResult:
    """The assembled result of building a Worker Context Pack."""

    sections: list[ContextPackSection] = field(default_factory=list)
    truncated: bool = False
    total_chars: int = 0
    content: str = ""
