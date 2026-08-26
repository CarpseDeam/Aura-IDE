"""The GUI's only owner of SkillLibrary access for the Skills manager.

Everything the manager knows about installed skills comes from
:class:`aura.skills.library.SkillLibrary`: the inventory, which entries are
effective in this workspace, what one skill contains, and every enable,
disable, or uninstall. This controller binds that library to the current
workspace, hands a chosen skill to the composer, and keeps the two in sync.

Two responsibilities are deliberately elsewhere. Turning library answers
into rows and details is
:mod:`aura.gui.skills_manager.presentation`'s, and one import at a time —
source, staging, review, installation — belongs to
:class:`aura.gui.skills_manager.import_controller.SkillImportController`,
which this controller owns, supplies an importer to, and refreshes from.

It deliberately re-derives nothing. Precedence, disabled state, workspace
markers, invalid entries, shadowing, path safety, conflict detection, and
validation are SkillLibrary's and SkillImporter's judgements; the manager
reports them. Creation receives only an injected normal-turn callback; this
facade never owns a provider client or hidden generation path.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from aura.gui.skills_manager.creation_controller import SkillCreationController
from aura.gui.skills_manager.creation_dialogs import SkillCreationPrompts
from aura.gui.skills_manager.import_controller import SkillImportController
from aura.gui.skills_manager.import_dialogs import ImportPrompts
from aura.gui.skills_manager.models import SCOPE_ORDER, SkillDetail, SkillRow
from aura.gui.skills_manager.presentation import build_detail, build_row
from aura.gui.skills_manager.redaction import redact_paths
from aura.gui.skills_manager.window import IMPORT_GITHUB, SkillsManagerWindow
from aura.skills.importer import SkillImporter
from aura.skills.library import SkillLibrary

logger = logging.getLogger(__name__)

_BUSY_MESSAGE = (
    "Aura is working on a turn right now. Wait until it finishes before "
    "importing, replacing, enabling, disabling, or uninstalling a skill — an "
    "active turn may still be reading one of these skills."
)

_IMPORT_BUSY_MESSAGE = (
    "Aura is importing a skill right now. Wait until that import finishes "
    "before changing the installed skills."
)

_CREATION_BUSY_MESSAGE = (
    "Aura is creating a skill right now. Finish or cancel that creation before "
    "starting another skill change."
)


class SkillsManagerController(QObject):
    """Owns the Skills window, its SkillLibrary access, and composer handoff."""

    creation_session_changed = Signal(bool)

    def __init__(
        self,
        *,
        input_panel,
        parent_widget: QWidget | None = None,
        workspace_root: Path | None = None,
        library_factory: Callable[[Path], SkillLibrary] | None = None,
        importer_factory: Callable[[SkillLibrary], SkillImporter] | None = None,
        import_prompts: ImportPrompts | None = None,
        creation_prompts: SkillCreationPrompts | None = None,
        start_creation_turn: Callable[[str, str], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._input = input_panel
        self._parent_widget = parent_widget
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._library_factory = library_factory or SkillLibrary
        self._importer_factory = importer_factory or SkillImporter
        self._window: SkillsManagerWindow | None = None
        self._rows: dict[str, SkillRow] = {}
        self._execution_active = False

        # One import at a time, owned outright: this controller supplies the
        # importer and reacts to the outcome, and knows nothing about staging.
        self._imports = SkillImportController(
            importer_factory=self._new_importer,
            prompts=import_prompts,
            dialog_parent=self._dialog_parent,
            parent=self,
        )
        self._imports.import_succeeded.connect(self._on_import_succeeded)
        self._imports.busy_changed.connect(self._on_import_busy_changed)
        self._imports.outstanding_job_changed.connect(self._on_import_job_changed)
        self._creation = SkillCreationController(
            import_controller=self._imports,
            start_turn=start_creation_turn,
            prompts=creation_prompts,
            dialog_parent=self._dialog_parent,
            workspace_root=self._workspace_root,
            parent=self,
        )
        self._creation.active_changed.connect(self._on_creation_active_changed)
        self._creation.busy_changed.connect(self._on_creation_busy_changed)

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

        An import belongs to the workspace it started in, so a rebind ends
        the session outright: its staged content is dropped and a result
        still in flight can no longer reach the new workspace.
        """
        self._creation.set_workspace_root(root)
        self._imports.abandon()
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
        self._sync_mutation_state()

    def shutdown(self) -> None:
        """Stop the import thread and drop staged content before teardown."""
        self._creation.shutdown()
        self._imports.shutdown()

    def creation_turn_finished(self, turn_id: str, *, successful: bool) -> None:
        """Forward the exact production-turn outcome to the creation owner."""
        self._creation.turn_finished(turn_id, successful=successful)

    def creation_active(self) -> bool:
        return self._creation.is_active()

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
        self._sync_mutation_state()
        window.set_rows(rows)

    def _sync_mutation_state(self) -> None:
        """Every mutation — import included — is off while either owner is busy."""
        if self._window is not None:
            self._window.set_mutations_enabled(self._mutations_available())

    def _mutations_available(self) -> bool:
        return (
            not self._execution_active
            and not self._imports.is_active()
            and not self._imports.has_outstanding_job()
            and not self._creation.is_active()
        )

    def _ensure_window(self) -> SkillsManagerWindow:
        if self._window is None:
            window = SkillsManagerWindow(self._parent_widget)
            window.current_row_changed.connect(self._on_current_row_changed)
            window.import_requested.connect(self._on_import_requested)
            window.create_requested.connect(self._on_create_requested)
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
        rows = [build_row(summary, effective_ids, selected_ids) for summary in summaries]
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
        return build_detail(row, inspection)

    # ---- import -------------------------------------------------------------

    def _new_importer(self) -> SkillImporter | None:
        """The importer one import session will use, bound to this workspace."""
        library = self._library()
        if library is None:
            return None
        try:
            return self._importer_factory(library)
        except Exception:
            logger.debug("skills manager: could not build SkillImporter", exc_info=True)
            return None

    def _on_import_requested(self, kind: str) -> None:
        """Start an import, or refuse it — including a programmatic request."""
        if not self._mutations_allowed():
            return
        if str(kind) == IMPORT_GITHUB:
            self._imports.start_github_import()
        else:
            self._imports.start_local_import()

    def _on_create_requested(self) -> None:
        """Start creation, including all guards for programmatic requests."""
        if not self._mutations_allowed():
            return
        self._creation.start()

    def _on_import_busy_changed(self, busy: bool, message: str) -> None:
        if self._window is not None:
            self._window.set_import_busy(bool(busy), message)
        self._sync_mutation_state()

    def _on_import_job_changed(self, _outstanding: bool) -> None:
        self._sync_mutation_state()

    def _on_creation_busy_changed(self, busy: bool, message: str) -> None:
        if self._window is not None:
            self._window.set_creation_busy(bool(busy), message)
        self._sync_mutation_state()
        self.creation_session_changed.emit(bool(busy))

    def _on_creation_active_changed(self, _active: bool) -> None:
        self._sync_mutation_state()

    def _on_import_succeeded(self, install_id: str) -> None:
        """Show what was just installed, without putting it in the composer.

        The user still chooses “Use in next message”, and an unsent chip for
        a skill that was just replaced in place is left exactly as it was.
        """
        self.refresh()
        window = self._window
        if window is None or not install_id:
            return
        if not window.select_row(install_id):
            # A search filter can hide a freshly installed row; clearing it
            # is the only way the reveal means anything.
            window.set_search_text("")
            window.select_row(install_id)

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
                f"Aura could not {verb} “{row.name}”: {redact_paths(exc)}",
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
                f"Aura could not uninstall “{row.name}”: {redact_paths(exc)}",
            )
            return
        self._remove_composer_chip(install_id)
        # A lower-precedence skill of the same name simply reappears on the
        # next scan; nothing here decides that.
        self.refresh()

    def _mutations_allowed(self) -> bool:
        """Refuse a mutation whenever someone else owns the skills on disk."""
        if self._execution_active:
            self._show_error("Skills", _BUSY_MESSAGE)
            return False
        if self._imports.is_active() or self._imports.has_outstanding_job():
            self._show_error("Skills", _IMPORT_BUSY_MESSAGE)
            return False
        if self._creation.is_active():
            self._show_error("Skills", _CREATION_BUSY_MESSAGE)
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


__all__ = ["SkillsManagerController"]
