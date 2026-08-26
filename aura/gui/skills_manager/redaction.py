"""One place where an absolute filesystem path stops being user-facing.

Both the manager facade and the import flow surface backend failures, and
both must do it without saying where Aura keeps skills. Library messages,
importer refusals, and diagnostics all carry real paths, so every one of
them passes through :func:`redact_paths` before it reaches a widget.
"""
from __future__ import annotations

import re

#: Absolute filesystem locations, wherever a backend message happens to
#: carry one — Windows drive letters, UNC roots, and POSIX roots alike.
_ABSOLUTE_PATH = re.compile(r"(?:^|(?<=[\s'\"(\[]))(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\")\]]*")


def redact_paths(text: object) -> str:
    """Return *text* with any absolute filesystem path replaced."""
    return _ABSOLUTE_PATH.sub("<path>", str(text or "")).strip()


__all__ = ["redact_paths"]
