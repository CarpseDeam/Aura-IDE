"""The GUI's only owner of SkillLibrary access for the Skills manager.

Everything the manager knows about installed skills comes from
:class:`aura.skills.library.SkillLibrary`: the inventory, which entries are
effective in this workspace, what one skill contains, and every enable,
disable, or uninstall. This controller binds that library to the current
workspace, translates its answers into the presentation shapes the window
renders, hands a chosen skill to the composer, and keeps the two in sync.

It deliberately re-derives nothing. Precedence, disabled state, workspace
markers, invalid entries, shadowing, and path safety are SkillLibrary's
judgements; the manager reports them. Nothing here writes a chat message or
calls a model — a lifecycle failure is a concise local dialog and stays one.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox, QWidget

from aura.gui.skills_manager.models import SCOPE_ORDER, SkillDetail, SkillRow, scope_label
from aura.gui.skills_manager.window import SkillsManagerWindow
from aura.skills.identity import InstalledSkillId, InstallScope
from aura.skills.library import InstalledSkillSummary, SkillInspection, SkillLibrary

logger = logging.getLogger(__name__)

_INVALID_DESCRIPTION = "This skill could not be read. Open it for the details."

_BUSY_MESSAGE = (
    "Aura is working on a turn right now. Wait until it finishes before "
    "enabling, disabling, or uninstalling a skill — an active turn may still "
    "be reading one of these skills."
)

#: Absolute filesystem locations, wherever a library message happens to carry
#: one. Where Aura keeps skills is not part of this surface, so a path is
#: redacted before anything reaches the window.
_ABSOLUTE_PATH = re.compile(r"(?:^|(?<=[\s'\"(\[]))(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\")\]]*")


def _redact_paths(text: object) -> str:
    """Return *text* with any absolute filesystem path replaced."""
    return _ABSOLUTE_PATH.sub("<path>", str(text or "")).strip()


class SkillsManagerController(QObject):
    """Owns the Skills window, its SkillLibrary access, and composer handoff."""

    def __init__(
        self,
        *,
        input_panel,
        parent_widget: QWidget | None = None,
        workspace_root: Path | None = None,
        library_factory: Callable[[Path], SkillLibrary] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._input = input_panel
        self._parent_widget = parent_widget
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._library_factory = library_factory or SkillLibrary
        self._window: SkillsManagerWindow | None = None
        self._rows: dict[str, SkillRow] = {}
        self._execution_active = False

        selection_changed = getattr(input_panel, "skill_selection_changed", None)
        self._tracks_composer_selection = selection_changed is not None
        if selection_changed is not None:
            selection_changed.connect(self._on_composer_selection_changed)

    # ---- lifecycle wiring --------------------------------------------------

    @property
    def window(self) -> SkillsManagerWindow | None:
        """The manager window, or None while it has never been opened."""
        return self._window

    def set_workspace_root(self, root: Path | None) -> None:
        """Rebind to a new workspace, discarding the previous one's inventory.

        Project skills belong to the workspace they are installed in, so the
        old rows are dropped outright rather than filtered; an open manager
        is rebuilt against the new workspace immediately.
        """
        self._workspace_root = Path(root) if root is not None else None
        self._rows = {}
        if self._window is not None:
            self._window.set_rows(())
            if self._window.isVisible():
                self.refresh()

    def set_execution_active(self, active: bool) -> None:
        """Allow browsing during a production turn, but never mutation.

        Uninstalling a skill an active turn already froze would remove the
        resource directory out from under it, so lifecycle actions are shut
        off for the duration. Inventory and composer selection stay live.
        """
        self._execution_active = bool(active)
        if self._window is not None:
            self._window.set_mutations_enabled(not self._execution_active)

    # ---- opening -----------------------------------------------------------

    def open_manager(self) -> None:
        """Open the one manager window, refreshed, raised, and focused."""
        if self._workspace_root is None:
            self._show_error(
                "Skills",
                "Open a project first — skills are managed per workspace.",
            )
            return
        window = self._ensure_window()
        self.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    def refresh(self) -> None:
        """Rebuild the inventory and detail from SkillLibrary."""
        window = self._window
        if window is None:
            return
        rows = self._build_rows()
        self._rows = {row.install_id: row for row in rows}
        window.set_mutations_enabled(not self._execution_active)
        window.set_rows(rows)

    def _ensure_window(self) -> SkillsManagerWindow:
        if self._window is None:
            window = SkillsManagerWindow(self._parent_widget)
            window.current_row_changed.connect(self._on_current_row_changed)
            window.use_requested.connect(self._on_use_requested)
            window.enable_toggle_requested.connect(self._on_enable_toggle_requested)
            window.uninstall_requested.connect(self._on_uninstall_requested)
            self._window = window
        return self._window

    # ---- inventory ---------------------------------------------------------

    def _library(self) -> SkillLibrary | None:
        """A library bound to the current workspace, or None without one."""
        if self._workspace_root is None:
            return None
        try:
            return self._library_factory(self._workspace_root)
        except Exception:
            logger.debug("skills manager: could not bind SkillLibrary", exc_info=True)
            return None

    def _build_rows(self) -> tuple[SkillRow, ...]:
        library = self._library()
        if library is None:
            return ()
        try:
            summaries = library.list_installed()
        except Exception:
            logger.debug("skills manager: list_installed failed", exc_info=True)
            self._show_error("Skills", "Aura could not read the installed skills.")
            return ()

        # The effective set is the only authority on what may be selected:
        # precedence, disabled state, and workspace applicability are already
        # resolved inside it.
        effective, _diagnostics = library.discover_effective_skills()
        effective_ids = {skill.install_id for skill in effective if skill.install_id}
        selected_ids = self._selected_install_ids()
        rows = [_row_for(summary, effective_ids, selected_ids) for summary in summaries]
        # Alphabetical within each scope, so a broken entry sits where its
        # name says it should rather than at the end of its group.
        rows.sort(key=lambda row: (SCOPE_ORDER.index(row.scope), row.name.lower()))
        return tuple(rows)

    def _selected_install_ids(self) -> set[str]:
        selected = getattr(self._input, "selected_skills", None)
        if not callable(selected):
            return set()
        return {skill.install_id for skill in selected()}

    # ---- detail ------------------------------------------------------------

    def _on_current_row_changed(self, install_id: str) -> None:
        window = self._window
        if window is None:
            return
        window.set_details(self._detail_for(install_id) if install_id else None)

    def _detail_for(self, install_id: str) -> SkillDetail | None:
        row = self._rows.get(install_id)
        library = self._library()
        if row is None or library is None:
            return None
        try:
            inspection = library.inspect(install_id)
        except Exception:
            logger.debug("skills manager: inspect failed for %s", install_id, exc_info=True)
            inspection = None
        return _detail_for(row, inspection)

    # ---- composer handoff --------------------------------------------------

    def _on_use_requested(self, install_id: str) -> None:
        row = self._rows.get(install_id)
        if row is None or not row.selectable:
            return
        try:
            self._input.select_installed_skill(row.install_id, row.name)
        except Exception:
            logger.debug("skills manager: composer selection failed", exc_info=True)
            self._show_error("Skills", f"Aura could not add “{row.name}” to your message.")
            return
        # The manager stays open so several skills can be added in one visit;
        # a panel that reports its own selection drives the refresh itself.
        if not self._tracks_composer_selection:
            self.refresh()

    def _on_composer_selection_changed(self) -> None:
        if self._window is not None and self._window.isVisible():
            self.refresh()

    def _remove_composer_chip(self, install_id: str) -> None:
        """Drop one unsent chip whose skill just stopped being usable."""
        remove = getattr(self._input, "remove_selected_skill", None)
        if callable(remove):
            remove(install_id)

    # ---- lifecycle actions -------------------------------------------------

    def _on_enable_toggle_requested(self, install_id: str, enabled: bool) -> None:
        row = self._rows.get(install_id)
        if row is None or not self._mutations_allowed():
            return
        library = self._library()
        if library is None:
            return
        verb = "enable" if enabled else "disable"
        try:
            library.set_enabled(install_id, enabled)
        except Exception as exc:
            logger.debug("skills manager: set_enabled failed for %s", install_id, exc_info=True)
            self._show_error(
                "Skills",
                f"Aura could not {verb} “{row.name}”: {_redact_paths(exc)}",
            )
            return
        if not enabled:
            self._remove_composer_chip(install_id)
        self.refresh()

    def _on_uninstall_requested(self, install_id: str) -> None:
        row = self._rows.get(install_id)
        if row is None or not row.can_uninstall or not self._mutations_allowed():
            return
        library = self._library()
        if library is None:
            return
        if not self._confirm_uninstall(row):
            return
        try:
            library.uninstall(install_id)
        except Exception as exc:
            logger.debug("skills manager: uninstall failed for %s", install_id, exc_info=True)
            self._show_error(
                "Skills",
                f"Aura could not uninstall “{row.name}”: {_redact_paths(exc)}",
            )
            return
        self._remove_composer_chip(install_id)
        # A lower-precedence skill of the same name simply reappears on the
        # next scan; nothing here decides that.
        self.refresh()

    def _mutations_allowed(self) -> bool:
        if self._execution_active:
            self._show_error("Skills", _BUSY_MESSAGE)
            return False
        return True

    def _confirm_uninstall(self, row: SkillRow) -> bool:
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Uninstall skill",
            f"Uninstall the {row.scope_label.lower()} skill “{row.name}”?\n\n"
            "It is deleted from disk and cannot be recovered from Aura.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self._dialog_parent(), title, message)

    def _dialog_parent(self) -> QWidget | None:
        if self._window is not None and self._window.isVisible():
            return self._window
        return self._parent_widget


def _row_for(
    summary: InstalledSkillSummary,
    effective_ids: set[str],
    selected_ids: set[str],
) -> SkillRow:
    usable = summary.installed_id in effective_ids
    description = summary.description or ("" if summary.valid else _INVALID_DESCRIPTION)
    return SkillRow(
        install_id=summary.installed_id,
        scope=summary.scope.value,
        name=summary.name,
        description=_redact_paths(description),
        status_text=_status_text(summary, usable),
        enabled=summary.enabled,
        valid=summary.valid,
        usable=usable,
        already_selected=summary.installed_id in selected_ids,
        can_uninstall=summary.scope != InstallScope.BUNDLED,
    )


def _status_text(summary: InstalledSkillSummary, usable: bool) -> str:
    """Say, in the user's terms, why a row is or is not in play."""
    if not summary.valid:
        return "Invalid"
    if not summary.enabled:
        return "Disabled"
    if summary.shadowed_by:
        winner = InstalledSkillId.parse(summary.shadowed_by)
        if winner is not None:
            return f"Shadowed by the {scope_label(winner.scope.value).lower()} skill"
        return "Shadowed"
    if not usable:
        return "Not available in this workspace"
    return "Enabled"


def _detail_for(row: SkillRow, inspection: SkillInspection | None) -> SkillDetail:
    description = row.description
    fields: list[tuple[str, str]] = []
    diagnostics: tuple[str, ...] = ()

    if inspection is not None:
        if inspection.description:
            description = inspection.description
        if inspection.model:
            fields.append(("Model", inspection.model))
        if inspection.task_kinds:
            fields.append(("Task kinds", ", ".join(inspection.task_kinds)))
        if inspection.path_globs:
            fields.append(("Paths", ", ".join(inspection.path_globs)))
        if inspection.triggers:
            fields.append(("Triggers", ", ".join(inspection.triggers)))
        if inspection.resource_entries:
            fields.append(("Resources", ", ".join(inspection.resource_entries)))
        elif inspection.has_resources:
            fields.append(("Resources", "included with this skill"))
        if inspection.body_chars:
            fields.append(("Instructions", f"{inspection.body_chars:,} characters"))
        diagnostics = tuple(
            f"{diagnostic.severity.value}: {diagnostic.code} — {_redact_paths(diagnostic.message)}"
            for diagnostic in inspection.diagnostics
        )

    return SkillDetail(
        install_id=row.install_id,
        name=row.name,
        scope_label=row.scope_label,
        status_text=row.status_text,
        description=_redact_paths(description),
        fields=tuple((label, _redact_paths(value)) for label, value in fields),
        diagnostics=diagnostics,
    )


__all__ = ["SkillsManagerController"]
