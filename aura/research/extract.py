"""Report parser for Playwright MCP page reports.

Parses the markdown-delimited report format produced by
Playwright MCP's browser_snapshot tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


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


def parse_report(raw: str) -> ParsedPage:
    """Parse a Playwright MCP report string into a ParsedPage.

    The report is split on ``\\n### `` section headers. Only the
    ``Page state`` section is used for URL, title, aria snapshot
    content, and links.
    """
    sections = raw.split("\n### ")
    # First section may start with "### " if the report opens with a header
    sections = [s[4:] if s.startswith("### ") else s for s in sections]

    # Locate the Page state section
    page_body: str | None = None
    for s in sections:
        first = s.split("\n", 1)[0] if "\n" in s else s
        if first.strip() == "Page state":
            _, _, body = s.partition("\n")
            page_body = body
            break

    if page_body is None:
        return ParsedPage()

    # Extract URL and Title metadata, then collect remaining aria lines
    url = ""
    title = ""
    aria_lines: list[str] = []

    in_meta = True
    for line in page_body.split("\n"):
        if in_meta:
            stripped = line.strip()
            if stripped.startswith("URL: "):
                url = stripped[5:].strip()
            elif stripped.startswith("Title: "):
                title = stripped[7:].strip()
            elif stripped == "":
                continue  # blank separator between meta and aria body
            else:
                in_meta = False
                aria_lines.append(line)
        else:
            aria_lines.append(line)

    # Parse links and build clean text from aria lines
    links: list[tuple[str, str]] = []
    clean_lines: list[str] = []

    i = 0
    while i < len(aria_lines):
        line = aria_lines[i]
        stripped = line.strip()

        if not stripped:
            clean_lines.append("")
            i += 1
            continue

        # Discard /url: lines entirely
        if stripped.startswith("/url:"):
            i += 1
            continue

        if stripped.startswith("- "):
            body = stripped[2:]

            # Detect aria link nodes
            link_match = re.match(r'link\s+"([^"]*)"', body)
            if link_match:
                visible_text = link_match.group(1)
                href = ""
                # Look ahead for a trailing /url: line
                if (i + 1 < len(aria_lines)
                        and aria_lines[i + 1].strip().startswith("/url:")):
                    m = re.match(r"/url:\s*(.*)", aria_lines[i + 1].strip())
                    if m:
                        href = m.group(1).strip()
                    i += 1  # consume the /url: line
                links.append((visible_text, href))

            cleaned = _clean_line(stripped)
            if cleaned:
                clean_lines.append(cleaned)
        else:
            clean_lines.append(stripped)

        i += 1

    collapsed = _collapse_blank_lines(clean_lines)
    clean_text = "\n".join(collapsed).strip()

    return ParsedPage(url=url, title=title, clean_text=clean_text, links=links)


def _clean_line(line: str) -> str:
    """Extract plain text content from a single aria node line."""
    if line.startswith("- "):
        line = line[2:]
    # Remove bracketed reference tokens  [ref=…]
    line = re.sub(r"\s*\[ref=\w+\]", "", line)
    # Extract the first quoted string as the visible text
    m = re.search(r'"([^"]*)"', line)
    if m:
        return m.group(1)
    return ""


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


def parse_call_result(result: dict[str, Any]) -> ParsedPage:
    """Parse an MCPClient.call_tool result dict into a ParsedPage."""
    if not result.get("ok", True):
        return ParsedPage(clean_text=f"MCP error: {result.get('error', '')}")
    content = result.get("content", [])
    raw = "\n".join(str(c) for c in content)
    return parse_report(raw)
