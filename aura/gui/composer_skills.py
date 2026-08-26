"""Visible installed-skill chips and ordered composer selection state."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

from aura.gui.theme import BG_RAISED, BORDER, DANGER, FG, FG_DIM
from aura.skills.identity import InstalledSkillId


@dataclass(frozen=True)
class ComposerSkill:
    """One immutable installed-skill selection captured by a send payload."""

    install_id: str
    label: str


class _SkillChip(QFrame):
    removed = Signal(str)

    def __init__(self, skill: ComposerSkill) -> None:
        super().__init__()
        self.skill = skill
        self.setStyleSheet(
            f"QFrame {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            "border-radius: 9px; padding: 1px 5px; }} "
            f"QFrame:hover {{ background: {BORDER}; border-color: {FG_DIM}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 3, 2)
        layout.setSpacing(4)

        label = QLabel(skill.label)
        label.setToolTip(skill.install_id)
        label.setStyleSheet(f"color: {FG}; background: transparent;")
        layout.addWidget(label)

        close = QToolButton()
        close.setText("x")
        close.setToolTip(f"Remove {skill.label}")
        close.setStyleSheet(
            f"QToolButton {{ background: transparent; color: {FG_DIM}; border: none; }} "
            f"QToolButton:hover {{ color: {DANGER}; }}"
        )
        close.clicked.connect(lambda: self.removed.emit(skill.install_id))
        layout.addWidget(close)


class ComposerSkillsWidget(QWidget):
    """Own the composer's visible chips and ordered installed-skill selection."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selection: list[ComposerSkill] = []
        self._chips: dict[str, _SkillChip] = {}

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._row.addStretch(1)
        self.setStyleSheet("background: transparent;")
        self.setVisible(False)

    @property
    def selection(self) -> tuple[ComposerSkill, ...]:
        """Return an immutable submission-time snapshot in visible order."""
        return tuple(self._selection)

    def select_installed_skill(self, install_id: str, label: str) -> bool:
        """Add one installed skill, returning whether the selection changed."""
        parsed = InstalledSkillId.parse(install_id)
        if parsed is None:
            raise ValueError("installed skill identity must use scope:name")
        canonical_id = str(parsed)
        if canonical_id in self._chips:
            return False

        skill = ComposerSkill(
            install_id=canonical_id,
            label=str(label or "").strip() or canonical_id,
        )
        chip = _SkillChip(skill)
        chip.removed.connect(self.remove_installed_skill)
        self._selection.append(skill)
        self._chips[canonical_id] = chip
        self._row.insertWidget(self._row.count() - 1, chip)
        self.setVisible(True)
        self.selection_changed.emit()
        return True

    def remove_installed_skill(self, install_id: str) -> bool:
        """Remove one selected identity without disturbing the remaining order."""
        chip = self._chips.pop(install_id, None)
        if chip is None:
            return False
        self._selection = [
            skill for skill in self._selection if skill.install_id != install_id
        ]
        self._row.removeWidget(chip)
        chip.deleteLater()
        self.setVisible(bool(self._selection))
        self.selection_changed.emit()
        return True

    def clear(self) -> None:
        """Remove every chip and selection."""
        if not self._selection:
            return
        self._selection.clear()
        chips = tuple(self._chips.values())
        self._chips.clear()
        for chip in chips:
            self._row.removeWidget(chip)
            chip.deleteLater()
        self.setVisible(False)
        self.selection_changed.emit()

    def restore(self, skills: tuple[ComposerSkill, ...]) -> None:
        """Replace the current selection from a rejected payload snapshot."""
        self.clear()
        for skill in skills:
            self.select_installed_skill(skill.install_id, skill.label)
