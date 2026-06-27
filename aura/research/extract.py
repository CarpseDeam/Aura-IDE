"""Text normalisation helpers for page content extraction.

Provides ``normalize_text`` and ``ParsedPage`` — used by
``playwright.py`` to clean up scraped page text.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def normalize_text(raw: str) -> str:
    """Strip trailing whitespace from each line, then collapse runs of consecutive blank lines."""
    lines = raw.split("\n")
    stripped = [line.rstrip() for line in lines]
    collapsed = _collapse_blank_lines(stripped)
    return "\n".join(collapsed)


@dataclass(frozen=True)
class ParsedPage:
    """Structured representation of a parsed Playwright page report."""

    url: str = ""
    title: str = ""
    clean_text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    """Collapse multiple consecutive blank lines into at most one."""
    result: list[str] = []
    was_blank = False
    for line in lines:
        if line == "":
            if not was_blank:
                result.append("")
                was_blank = True
        else:
            result.append(line)
            was_blank = False
    return result
