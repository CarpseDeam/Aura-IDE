"""The user-facing Skills manager: one controller, one reusable window."""
from __future__ import annotations

from aura.gui.skills_manager.controller import SkillsManagerController
from aura.gui.skills_manager.models import SkillDetail, SkillRow
from aura.gui.skills_manager.window import SkillsManagerWindow

__all__ = [
    "SkillDetail",
    "SkillRow",
    "SkillsManagerController",
    "SkillsManagerWindow",
]
