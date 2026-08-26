"""Safe resolution of one skill's supporting resource files.

Shared by :class:`aura.skills.library.SkillLibrary` (the general lifecycle
operation) and the production ``read_skill_resource`` tool (which additionally
requires the skill to be activated in the frozen turn — see
:mod:`aura.skills.turn_state`). This module only ever answers "is this path a
real file inside this one skill's directory," never anything about turn
state or activation.
"""
from __future__ import annotations

from pathlib import Path

from aura.paths import first_link_like_component, is_link_like, safe_is_relative_to


class SkillResourceError(Exception):
    """A requested resource path is invalid, unsafe, or does not exist."""


def resolve_skill_resource(source_dir: Path, relative_path: str) -> Path:
    """Resolve *relative_path* against *source_dir*, or raise SkillResourceError.

    Rejects absolute paths, ``..`` traversal, any symlink or junction hop
    (even one that would resolve back inside the directory — resources are
    read exactly as they were installed), and anything that is not a regular
    file. The skill's own root is checked *before* it is resolved, so a
    linked skill directory can never contribute its link target as a trusted
    root. The final containment check additionally catches a link whose
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

    unresolved_root = Path(source_dir)
    if is_link_like(unresolved_root):
        raise SkillResourceError(
            f"skill directory '{unresolved_root}' is a symlink or junction; its resources are not readable"
        )
    if first_link_like_component(unresolved_root, candidate.parts) is not None:
        raise SkillResourceError(f"path '{relative_path}' passes through a symlink or junction")

    root = unresolved_root.resolve()
    try:
        resolved = (unresolved_root / candidate).resolve()
    except OSError as exc:
        raise SkillResourceError(f"could not resolve '{relative_path}': {exc}") from exc

    if not safe_is_relative_to(resolved, root):
        raise SkillResourceError(f"path '{relative_path}' escapes the skill directory")
    if not resolved.exists():
        raise SkillResourceError(f"'{relative_path}' does not exist in this skill")
    if not resolved.is_file():
        raise SkillResourceError(f"'{relative_path}' is not a regular file")
    return resolved
