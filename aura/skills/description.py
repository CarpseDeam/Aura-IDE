"""Deterministic skill descriptions for the progressive-disclosure index.

Every candidate in the initial skill index needs a concise one-line
description.  Authors may declare ``description`` in the front matter of a
markdown skill or in a JSON skill object; when none is declared — or the
declared value is malformed — a deterministic fallback is derived from the
body's first heading and first meaningful paragraph.  No model is ever called
to summarise a skill.

The fallback is a pure local extraction, so it is stable across recompositions
and retries: identical body text always yields the identical description.
"""

from __future__ import annotations

import re

#: Index-size safeguard, deliberately not relevance intelligence.  It bounds
#: one skill's description so a pathological body (or a hostile over-long
#: authored description) cannot blow up the initial skill index; it never
#: decides whether a skill is relevant.
_MAX_DESCRIPTION_CHARS: int = 160


def _strip_markdown_markers(text: str) -> str:
    """Remove a leading heading marker or bullet from a single line."""
    stripped = text.strip()
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    if stripped.startswith("- ") or stripped.startswith("* "):
        return stripped[2:].strip()
    return stripped


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = _strip_markdown_markers(stripped)
            if heading:
                return heading
    return ""


def _first_meaningful_paragraph(body: str) -> str:
    """First non-heading line long enough to describe the skill.

    Bullet prefixes are stripped so a list item reads as prose.  A heading or
    an empty line is skipped.  A line shorter than the length floor is not
    meaningful prose (likely a bare label), so it is skipped.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = _strip_markdown_markers(stripped)
        if len(candidate) >= 20:
            return candidate
    return ""


def _normalize_compact(text: str) -> str:
    """Collapse whitespace/newlines into one compact line."""
    return re.sub(r"\s+", " ", text).strip()


def _bounded(text: str) -> str:
    compact = _normalize_compact(text)
    if len(compact) <= _MAX_DESCRIPTION_CHARS:
        return compact
    return compact[:_MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"


def derive_skill_description(
    body: str,
    *,
    explicit: str | None = None,
) -> str:
    """Return the one-line description used for a skill's index entry.

    ``explicit`` is the authored ``description`` value when one exists.  A
    malformed explicit value (empty after stripping) falls through to the
    deterministic body fallback.  When the body yields nothing, an empty
    string is returned — never an exception, so prompt composition cannot
    crash on a skill whose body is opaque.
    """
    if explicit is not None:
        cleaned = _normalize_compact(explicit)
        if cleaned:
            return _bounded(cleaned)

    heading = _first_heading(body)
    paragraph = _first_meaningful_paragraph(body)
    if heading and paragraph:
        return _bounded(f"{heading}: {paragraph}")
    if heading:
        return _bounded(heading)
    if paragraph:
        return _bounded(paragraph)
    return ""
