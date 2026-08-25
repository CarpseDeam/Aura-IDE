"""Stable installed-skill identity, separate from the hashed per-turn candidate id.

An installed skill (project/personal/bundled) is identified by ``scope:name`` —
stable across body edits, suitable for enable/disable, replacement, and
uninstall. The frozen per-turn candidate id
(:func:`aura.skills.models.compute_skill_id`) stays content-addressed and
serves a different purpose (proving a loaded body is the exact body a frozen
turn's index described); :data:`aura.skills.models.Skill.install_id` is the
one explicit mapping between the two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Duplicate-name precedence, most to least authoritative.
SCOPE_PRECEDENCE: tuple[str, ...] = ("project", "personal", "bundled")


class InstallScope(str, Enum):
    """Where an installed skill's SKILL.md folder lives.

    Distinct from :class:`aura.skills.models.SkillProvenance`: provenance is
    the coarse runtime category (bundled vs. user-authored) that selection
    and prompt composition have always used; scope is the finer-grained
    installation location a library lifecycle operation acts on.
    """

    PROJECT = "project"
    PERSONAL = "personal"
    BUNDLED = "bundled"


@dataclass(frozen=True)
class InstalledSkillId:
    """Stable ``scope:name`` identity for one installed skill."""

    scope: InstallScope
    name: str

    def __str__(self) -> str:
        return f"{self.scope.value}:{self.name}"

    @classmethod
    def parse(cls, raw: str) -> "InstalledSkillId | None":
        """Parse a ``scope:name`` string, or return None if malformed."""
        if not isinstance(raw, str) or ":" not in raw:
            return None
        scope_text, _, name = raw.partition(":")
        try:
            scope = InstallScope(scope_text.strip())
        except ValueError:
            return None
        name = name.strip()
        if not name:
            return None
        return cls(scope=scope, name=name)


def is_valid_skill_name(name: str) -> bool:
    """True for a normalized lowercase-kebab-case skill name."""
    return bool(name) and bool(_NAME_RE.match(name))


def normalize_skill_name(raw: str) -> str:
    """Best-effort normalization of a candidate name into kebab-case.

    Used only to derive a default name from a folder or file name; declared
    ``name:`` front matter is validated as-is, never silently rewritten.
    """
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
