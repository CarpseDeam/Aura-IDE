"""Presentation shapes the skills controller hands to its window.

Every judgement these carry — precedence, disabled state, workspace
applicability, whether an entry may be uninstalled — was already made by
:class:`aura.skills.library.SkillLibrary` and merely translated by the
controller. The window renders them and never re-derives any of it from
disk, so there is exactly one discovery implementation in the product.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Scope keys in the order the manager groups them, matching SkillLibrary's
#: own project > personal > bundled precedence order.
SCOPE_ORDER: tuple[str, ...] = ("project", "personal", "bundled")

SCOPE_LABELS: dict[str, str] = {
    "project": "Project",
    "personal": "Personal",
    "bundled": "Bundled",
}


def scope_label(scope: str) -> str:
    """Human label for one install scope key."""
    return SCOPE_LABELS.get(str(scope), str(scope).title())


@dataclass(frozen=True)
class SkillRow:
    """One installed skill as the manager list shows it.

    ``usable`` mirrors membership of SkillLibrary's effective set for this
    workspace — the single source of truth for whether the exact installed
    identity can be added to a message. A row that is not usable stays
    visible and inspectable; it just cannot be selected.
    """

    install_id: str
    scope: str
    name: str
    description: str
    status_text: str
    enabled: bool
    valid: bool
    usable: bool
    already_selected: bool
    can_uninstall: bool

    @property
    def scope_label(self) -> str:
        return scope_label(self.scope)

    @property
    def selectable(self) -> bool:
        """True when this exact identity can still be added to the composer."""
        return self.usable and not self.already_selected


@dataclass(frozen=True)
class SkillDetail:
    """Everything the detail pane shows for the current row.

    Carries no filesystem location: a source directory is an implementation
    detail of where Aura keeps skills, never something the manager renders.
    """

    install_id: str
    name: str
    scope_label: str
    status_text: str
    description: str
    fields: tuple[tuple[str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()


__all__ = [
    "SCOPE_LABELS",
    "SCOPE_ORDER",
    "SkillDetail",
    "SkillRow",
    "scope_label",
]
