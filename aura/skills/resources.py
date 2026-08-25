"""Safe resolution of one skill's supporting resource files.

Shared by :class:`aura.skills.library.SkillLibrary` (the general lifecycle
operation) and the production ``read_skill_resource`` tool (which additionally
requires the skill to be activated in the frozen turn — see
:mod:`aura.skills.turn_state`). This module only ever answers "is this path a
real file inside this one skill's directory," never anything about turn
state or activation.
"""
from __future__ import annotations

import os
from pathlib import Path

from aura.paths import safe_is_relative_to


class SkillResourceError(Exception):
    """A requested resource path is invalid, unsafe, or does not exist."""


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def resolve_skill_resource(source_dir: Path, relative_path: str) -> Path:
    """Resolve *relative_path* against *source_dir*, or raise SkillResourceError.

    Rejects absolute paths, ``..`` traversal, any symlink or junction hop
    (even one that would resolve back inside the directory — resources are
    read exactly as they were installed), and anything that is not a regular
    file. The final containment check additionally catches a link whose
    target only escapes once the OS has normalised it.
    """
    text = str(relative_path or "").strip().strip("/\\")
    if not text:
        raise SkillResourceError("path must not be empty")

    candidate = Path(text)
    if candidate.is_absolute():
        raise SkillResourceError(f"path '{relative_path}' must be relative to the skill directory")
    if ".." in candidate.parts:
        raise SkillResourceError(f"path '{relative_path}' contains '..'")

    root = Path(source_dir).resolve()
    walked = root
    for part in candidate.parts:
        walked = walked / part
        if walked.exists(follow_symlinks=False) and _is_link_like(walked):
            raise SkillResourceError(f"path '{relative_path}' passes through a symlink or junction")

    try:
        resolved = walked.resolve()
    except OSError as exc:
        raise SkillResourceError(f"could not resolve '{relative_path}': {exc}") from exc

    if not safe_is_relative_to(resolved, root):
        raise SkillResourceError(f"path '{relative_path}' escapes the skill directory")
    if not resolved.exists():
        raise SkillResourceError(f"'{relative_path}' does not exist in this skill")
    if not resolved.is_file():
        raise SkillResourceError(f"'{relative_path}' is not a regular file")
    return resolved
