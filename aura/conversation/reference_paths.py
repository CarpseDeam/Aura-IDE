"""Detect explicit external project directories in user-authored text.

This module deliberately does not decide whether a candidate is safe to use.
It only extracts absolute path-shaped text and applies the filesystem facts
needed to distinguish an external directory from an ordinary workspace path.
ReferenceRootAccess remains the authorization and containment boundary.
"""
from __future__ import annotations

import re
from pathlib import Path

from aura.paths import safe_is_relative_to


class ReferencePathError(ValueError):
    """The user explicitly supplied an unusable external reference path."""


# Quoting is the unambiguous way to paste a path containing spaces. Backticks
# are included because they are common in coding-harness messages.
_QUOTED_PATH = re.compile(r'(?P<quote>["`])(?P<path>[^"`\r\n]+)(?P=quote)')
# Bare paths intentionally stop at whitespace. A user can still paste a
# space-containing path by quoting/backticking it, while ordinary prose after
# a path is not swallowed into the candidate.
_BARE_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/])(?P<path>(?:[A-Za-z]:[\\/]|/)[^\s<>\"'“”‘’]+)"
)

_TRAILING_PUNCTUATION = ".,;!?)]}"


def _trim_candidate(raw: str) -> str:
    return raw.strip().strip(_TRAILING_PUNCTUATION)


def _is_absolute_path_text(raw: str) -> bool:
    """Recognize native and Windows absolute syntax on every host."""
    return Path(raw).is_absolute() or bool(
        re.match(r"^(?:[A-Za-z]:[\\/]|/)", raw)
    )


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


def extract_reference_path(
    text: str | None,
    workspace_root: Path,
) -> Path | None:
    """Extract one valid external directory from raw user-authored text.

    ``None`` means the user supplied no external reference directory. An
    explicit external path that is missing or a file raises
    :class:`ReferencePathError`; so does more than one distinct valid external
    directory. Absolute paths inside the active workspace are ignored because
    they do not authorize an external reference.
    """
    workspace = workspace_root.resolve()
    valid_external: dict[Path, str] = {}
    invalid_external: list[str] = []

    for raw in extract_absolute_path_candidates(text):
        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError) as exc:
            raise ReferencePathError(
                f"The external reference path could not be resolved: {raw} ({exc})."
            ) from exc

        # Absolute paths that point into the editable workspace are ordinary
        # workspace references, never external authorization candidates.
        inside_workspace = safe_is_relative_to(resolved, workspace)
        if inside_workspace:
            continue

        if not resolved.exists():
            invalid_external.append(raw)
            continue
        if not resolved.is_dir():
            invalid_external.append(raw)
            continue
        valid_external.setdefault(resolved, raw)

    if invalid_external:
        path = invalid_external[0]
        raise ReferencePathError(
            f"External reference path must be an existing directory: {path}."
        )

    if len(valid_external) > 1:
        raise ReferencePathError(
            "Aura currently supports one external reference project per request."
        )

    return next(iter(valid_external), None)
