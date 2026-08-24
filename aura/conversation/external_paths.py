"""Detect explicit external file and directory paths in user-authored text.

This module deliberately does not decide whether a candidate is safe to use.
It only extracts absolute path-shaped text and applies the filesystem facts
needed to distinguish an external location from an ordinary workspace path.
:class:`aura.conversation.tools.external_read.ExternalReadAccess` remains the
authorization and containment boundary.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath

from aura.paths import safe_is_relative_to

# Quoting is the unambiguous way to paste a path containing spaces. Backticks
# are included because they are common in coding-harness messages.
_QUOTED_PATH = re.compile(r'(?P<quote>["`])(?P<path>[^"`\r\n]+)(?P=quote)')
# Bare paths intentionally stop at whitespace. A user can still paste a
# space-containing path by quoting/backticking it, while ordinary prose after
# a path is not swallowed into the candidate. Aura is a Windows harness, so a
# bare candidate must begin with a drive root or a conventional backslash UNC
# prefix; a leading forward slash is ordinary prompt text, not authority.
_BARE_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/])(?P<path>(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"'“”‘’]+)"
)

_TRAILING_PUNCTUATION = ".,;!?)]}"


def _trim_candidate(raw: str) -> str:
    return raw.strip().strip(_TRAILING_PUNCTUATION)


def _is_absolute_path_text(raw: str) -> bool:
    """Recognize explicit Windows absolute syntax on every host."""
    has_windows_root = bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", raw))
    return has_windows_root and PureWindowsPath(raw).is_absolute()


def extract_absolute_path_candidates(text: str | None) -> list[str]:
    """Return distinct absolute path-shaped strings in *text*, in order.

    The function is intentionally syntax-only. It does not inspect the
    filesystem and therefore is useful for tests and for keeping parsing
    separate from authorization.
    """
    if not text:
        return []

    matches: list[tuple[int, str]] = []
    quoted_spans: list[tuple[int, int]] = []
    for match in _QUOTED_PATH.finditer(str(text)):
        candidate = _trim_candidate(match.group("path"))
        if _is_absolute_path_text(candidate):
            matches.append((match.start(), candidate))
            quoted_spans.append(match.span())

    for match in _BARE_PATH.finditer(str(text)):
        if any(start <= match.start() < end for start, end in quoted_spans):
            continue
        candidate = _trim_candidate(match.group("path"))
        if _is_absolute_path_text(candidate):
            matches.append((match.start(), candidate))

    candidates: list[str] = []
    for _position, candidate in sorted(matches):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def extract_external_read_paths(
    text: str | None,
    workspace_root: Path,
) -> list[Path]:
    """Return the existing external locations the user explicitly named.

    Every absolute path-shaped string in the literal user text is resolved
    against the filesystem. Existing files and directories outside the active
    workspace are returned, in the order they appeared; each one authorizes
    read-only access to itself for the turn being prepared.

    Paths inside the active workspace are ignored: they are ordinary workspace
    references and already resolvable. A named path that does not exist — or
    that cannot be resolved at all — authorizes nothing and is dropped rather
    than failing the turn; the model simply cannot read it, exactly as with
    any other path it was not given.
    """
    workspace = workspace_root.resolve()
    authorized: list[Path] = []
    seen: set[str] = set()

    for raw in extract_absolute_path_candidates(text):
        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
            exists = resolved.exists()
        except (OSError, ValueError):
            continue
        if not exists:
            continue
        # Absolute paths that point into the editable workspace are ordinary
        # workspace references, never external authorization candidates.
        if safe_is_relative_to(resolved, workspace):
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        authorized.append(resolved)

    return authorized
