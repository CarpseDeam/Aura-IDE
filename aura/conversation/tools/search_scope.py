"""Workspace jail and sensitive-path policy shared by the search tools.

``grep_search`` and ``find_usages`` return *file contents* to the model.  That
makes them the widest read surface in the tool catalog: unlike ``read_file``
they take a pattern rather than a path, so nothing about the call names the
files it will open.  Two properties have to hold no matter what pattern
arrives.

**The workspace is a jail.**  An ``include_pattern`` is a workspace-relative
scope, never a way to name a location.  ``root / include_pattern`` silently
discards ``root`` when the pattern is absolute, and ``..`` walks straight out,
so both are rejected here rather than resolved.  Results are jailed a second
time on the way out: a symlink inside the workspace can still point outside
it, and a matched line is only reportable if the file it came from is really
under the root.

**Some files inside the workspace are still not search material.**  A ``.env``
or a private key is exactly what a broad pattern sweeps up, and a tool result
goes into the model's context and the transcript.  Those files are skipped and
*counted* — the skip appears in ``skipped_details`` so the result stays
truthful about what was not searched, rather than quietly reporting no match.

Both engines (ripgrep and the Python fallback) apply the same policy, so which
binary happens to be installed never changes what is reachable.

The sensitive-file classification itself (``is_sensitive_path``) is owned by
:mod:`aura.repository_inventory` so that grep and every repository index
share one authority; it is re-exported here rather than duplicated.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from aura.paths import safe_is_relative_to
from aura.repository_inventory import is_sensitive_path  # noqa: F401 — re-exported

#: Reason recorded in ``skipped_details`` for a policy-skipped file.
SENSITIVE_SKIP_REASON = "sensitive file (secrets are not searchable)"

#: Reason recorded when a path resolved outside the workspace root.
ESCAPED_SKIP_REASON = "resolved outside the workspace root"


def _has_parent_ref(text: str) -> bool:
    return ".." in PurePosixPath(text).parts


def resolve_include_scope(
    root: Path,
    include_pattern: str | None,
) -> tuple[list[Path] | None, str | None, str | None]:
    """Resolve *include_pattern* to ``(exact_files, glob, error)``.

    Exactly one of the three is meaningful: a pattern naming an existing file
    inside the workspace yields ``exact_files``; anything else usable yields a
    ``glob`` for the engine to apply; a pattern that tries to leave the
    workspace yields an ``error`` and no scope at all.

    Rejecting rather than clamping is deliberate — silently searching the
    workspace root after being handed ``/etc`` would answer a question nobody
    asked.
    """
    if not include_pattern:
        return None, None, None

    text = str(include_pattern).strip().replace("\\", "/")
    if not text:
        return None, None, None

    if PurePosixPath(text).is_absolute() or (len(text) > 1 and text[1] == ":"):
        return None, None, (
            f"include_pattern {include_pattern!r} is an absolute path; "
            "include_pattern is a workspace-relative scope"
        )
    if _has_parent_ref(text):
        return None, None, (
            f"include_pattern {include_pattern!r} contains '..'; "
            "search cannot leave the workspace root"
        )

    exact = root / text
    try:
        is_file = exact.is_file()
    except OSError:
        is_file = False
    if is_file and path_within_root(exact, root):
        return [exact], None, None

    return None, text, None


def path_within_root(path: Path, root: Path) -> bool:
    """True when *path* really resolves inside *root*.

    Applied to every result path, so a symlink that points out of the
    workspace cannot smuggle content back in through a match.
    """
    try:
        return safe_is_relative_to(path.resolve(), Path(root).resolve())
    except OSError:
        return False


__all__ = [
    "ESCAPED_SKIP_REASON",
    "SENSITIVE_SKIP_REASON",
    "is_sensitive_path",
    "path_within_root",
    "resolve_include_scope",
]
