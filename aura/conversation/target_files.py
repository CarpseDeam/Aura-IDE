"""Deterministic extraction of the target files a user request names.

The user usually supplies the edit surface in the request itself:

    fix the retry leak in aura/gui/send_handler.py and tests/test_send_handler.py

Losing those paths makes Aura start generic — it rediscovers an edit surface the
user already handed it. This module preserves that scope so the turn can carry
it into context composition.

The one rule this module obeys:

    Preserve paths the user actually named. Never guess.

A token becomes a target only when it survives every check:

* it is written in the request (or in an attachment reference),
* it resolves to a file that really exists,
* that file lies inside the workspace.

Nothing here searches the workspace, expands globs, or infers likely files from
a bare topic word. A request that names no paths yields no targets, which is the
correct answer — not a reason to go looking.
"""
from __future__ import annotations

import logging
from pathlib import Path

import re

_log = logging.getLogger(__name__)

__all__ = ["extract_target_files"]


# A path-shaped token: an optional Windows drive, optional directory segments,
# then a filename carrying an extension. Requiring the extension keeps ordinary
# prose out; the filesystem check below is what actually decides.
_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?"
    r"(?:[\w.\-]+[\\/])*"
    r"[\w.\-]+\.[A-Za-z0-9]{1,8}"
)

# Characters that commonly wrap a path in prose or markdown and are never part
# of one: backticks, quotes, brackets, and trailing sentence punctuation.
_TRIM_CHARS = "`'\"()[]{}<>,;:!?*"

# A pasted traceback or directory listing can name a great many real files. The
# turn's scope is the handful the user is working on, so keep extraction bounded
# rather than handing hundreds of files to preload.
_MAX_TARGET_FILES = 12


def extract_target_files(
    text: str | None,
    workspace_root: Path | None,
) -> tuple[str, ...]:
    """Return workspace-relative paths explicitly named in ``text``.

    Paths are returned in the order they appear, deduplicated, as POSIX-style
    workspace-relative strings. Anything that does not resolve to a real file
    inside ``workspace_root`` is dropped — including absolute paths pointing
    elsewhere on disk and ``..`` escapes.
    """
    if not text or workspace_root is None:
        return ()
    try:
        root = workspace_root.resolve()
    except OSError:
        return ()

    found: list[str] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(0).strip(_TRIM_CHARS)
        if not token:
            continue
        relpath = _resolve_within_workspace(token, root)
        if relpath is None or relpath in seen:
            continue
        seen.add(relpath)
        found.append(relpath)
        if len(found) >= _MAX_TARGET_FILES:
            _log.info(
                "target_file_extraction capped at %d files", _MAX_TARGET_FILES
            )
            break
    return tuple(found)


def _resolve_within_workspace(token: str, root: Path) -> str | None:
    """Resolve one token to a workspace-relative file path, or None.

    Returns None when the token is not a real file, or resolves outside the
    workspace. A bare filename is accepted only when it sits directly at the
    workspace root — resolving it anywhere else would mean searching, and a
    search can pick the wrong file.
    """
    candidate = token.replace("\\", "/").strip("/")
    if not candidate:
        return None

    path = Path(token.replace("\\", "/"))
    full = path if path.is_absolute() else root / candidate

    try:
        resolved = full.resolve()
        if not resolved.is_file():
            return None
        # relative_to on the resolved paths rejects both absolute paths outside
        # the workspace and ".." escapes from inside it.
        return resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
