"""Owns one skill import at a time, from source choice to installed skill.

The manager facade knows about installed skills; this controller knows about
the one import that is currently in flight. It collects the source and the
destination scope, runs staging and installation off the GUI thread through
:class:`aura.gui.skills_manager.import_worker.ImportJobRunner`, holds the
single :class:`~aura.skills.importer.ImportPreview` a session may own, shows
it for review, and installs only what the user explicitly approved.

It re-derives nothing. Discovery, conflict detection, validation, archive
and GitHub handling, fingerprinting, and atomic installation are all
:class:`~aura.skills.importer.SkillImporter`'s, reached through the importer
the facade supplies. A preview's ``conflict`` flag decides which action is
*offered*; ``replace=True`` is passed only after the user reviewed that
conflicting preview and chose to replace, and a conflict that appears after
the preview is the importer's refusal to surface — never a reason to retry.

Staging is temporary and must not survive its session. While an approved
install is running, its job owns the preview and cleans it in ``finally``;
otherwise the controller cleans defensively on cancel, abandonment, a late
preview, workspace rebind, and shutdown. ``cleanup`` is idempotent.
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from aura.gui.skills_manager.import_dialogs import ImportPrompts
from aura.gui.skills_manager.import_models import (
    SOURCE_FOLDER,
    SOURCE_GITHUB,
    SOURCE_ZIP,
    ImportDecision,
    ImportSource,
    build_preview_view,
)
from aura.gui.skills_manager.import_worker import ImportJobRunner
from aura.gui.skills_manager.redaction import redact_paths
from aura.skills.importer import ImportPreview, SkillImporter

logger = logging.getLogger(__name__)

_ERROR_TITLE = "Import skill"

_NO_WORKSPACE = "Open a project first — skills are imported into a workspace."

_ALREADY_RUNNING = "Aura is already importing a skill. Wait for that import to finish."

_GENERIC_FAILURE = "Aura could not import that skill."

#: Appended to an install refusal so the user knows the import is over. A
#: conflict that appeared after the preview is never retried as a replacement.
_REVIEW_AGAIN = "Run the import again to review the current state before installing."

_PHASE_PREVIEW = "preview"
_PHASE_INSTALL = "install"


class SkillImportController(QObject):
    """One import session: source, staging, preview, review, installation."""

    #: Emitted with the installed id after a successful import.
    import_succeeded = Signal(str)
    #: Emitted whenever the session's busy state changes, with an honest
    #: description of what is happening while it is True.
    busy_changed = Signal(bool, str)

    def __init__(
        self,
        *,
        importer_factory: Callable[[], SkillImporter | None],
        prompts: ImportPrompts | None = None,
        dialog_parent: Callable[[], QWidget | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._importer_factory = importer_factory
        self._prompts = prompts if prompts is not None else ImportPrompts()
        self._dialog_parent = dialog_parent or (lambda: None)
        self._runner = ImportJobRunner(self)
        self._runner.finished.connect(self._on_job_finished)

        self._token = 0
        self._active = False
        self._busy = False
        self._phase = ""
        self._source: ImportSource | None = None
        self._preview: ImportPreview | None = None
        self._importer: SkillImporter | None = None
        #: Importers for abandoned sessions whose job is still running, kept
        #: until every kind of late result releases its session state.
        self._retired: dict[int, SkillImporter] = {}

    # ---- state -------------------------------------------------------------

    def is_active(self) -> bool:
        """True while a session owns a question, staged content, or an install."""
        return self._active

    # ---- entry points ------------------------------------------------------

    def start_local_import(self) -> bool:
        """Import a skill from a folder or a ZIP archive the user picks."""
        importer = self._begin_guard()
        if importer is None:
            return False
        parent = self._dialog_parent()
        kind = self._prompts.ask_local_source_kind(parent)
        if kind not in (SOURCE_FOLDER, SOURCE_ZIP):
            return False
        chosen = (
            self._prompts.ask_folder(parent)
            if kind == SOURCE_FOLDER
            else self._prompts.ask_zip(parent)
        )
        location = str(chosen or "").strip()
        if not location:
            return False
        return self._start(importer, kind, location, _local_label(location))

    def start_github_import(self) -> bool:
        """Install a skill from a public GitHub repository or directory URL."""
        importer = self._begin_guard()
        if importer is None:
            return False
        url = str(self._prompts.ask_github_url(self._dialog_parent()) or "").strip()
        if not url:
            return False
        return self._start(importer, SOURCE_GITHUB, url, url)

    def _begin_guard(self) -> SkillImporter | None:
        """The importer this session will use, or None if it may not start."""
        if self._active:
            self._error(_ALREADY_RUNNING)
            return None
        importer = self._importer_factory()
        if importer is None:
            self._error(_NO_WORKSPACE)
            return None
        return importer

    def _start(self, importer: SkillImporter, kind: str, location: str, label: str) -> bool:
        # Asked last, so the destination question is the one immediately
        # before anything is staged.
        scope = self._prompts.ask_scope(self._dialog_parent())
        if scope is None:
            return False

        source = ImportSource(kind=kind, location=location, label=label, scope=scope)
        self._token += 1
        self._active = True
        self._phase = _PHASE_PREVIEW
        self._source = source
        self._importer = importer
        self._preview = None
        self._set_busy(True, f"Preparing “{label}”…")
        if not self._runner.start(self._token, _staging_job(importer, source)):
            self._finish(cleanup=False)
            self._error(_ALREADY_RUNNING)
            return False
        return True

    # ---- results -----------------------------------------------------------

    def _on_job_finished(self, token: int, result: object, error: object) -> None:
        if token != self._token or not self._active:
            self._discard_late(token, result)
            return
        if self._phase == _PHASE_PREVIEW:
            self._on_preview_finished(token, result, error)
        else:
            self._on_install_finished(result, error)

    def _discard_late(self, token: int, result: object) -> None:
        """Clean a result whose session was abandoned, without showing it."""
        importer = self._retired.pop(token, None)
        if isinstance(result, ImportPreview):
            _cleanup(importer, result)

    def _on_preview_finished(self, token: int, result: object, error: object) -> None:
        self._set_busy(False, "")
        if error is not None or not isinstance(result, ImportPreview):
            self._finish(cleanup=True)
            self._error(redact_paths(error) or _GENERIC_FAILURE)
            return
        source = self._source
        if source is None:
            self._discard_late(token, result)
            return

        self._preview = result
        view = build_preview_view(result, source)
        decision = self._prompts.review(self._dialog_parent(), view)
        if token != self._token or not self._active:
            # The session was abandoned while the review was open; whoever
            # abandoned it already cleaned the staged content up.
            return
        if decision is ImportDecision.CANCEL or not view.installable:
            self._finish(cleanup=True)
            return
        self._install(token, replace=decision is ImportDecision.REPLACE)

    def _install(self, token: int, *, replace: bool) -> None:
        importer = self._importer
        preview = self._preview
        if importer is None or preview is None:
            self._finish(cleanup=True)
            return
        self._phase = _PHASE_INSTALL
        self._set_busy(True, f"Installing “{preview.name}”…")
        if not self._runner.start(token, _install_job(importer, preview, replace=replace)):
            self._finish(cleanup=True)
            self._error(_ALREADY_RUNNING)
            return
        # The job now owns this preview through its captured callable. Dropping
        # the controller's reference prevents abandonment from cleaning staging
        # while install() is still validating or copying from it.
        self._preview = None

    def _on_install_finished(self, result: object, error: object) -> None:
        if error is not None:
            self._finish(cleanup=True)
            self._error(f"{redact_paths(error) or _GENERIC_FAILURE} {_REVIEW_AGAIN}")
            return
        installed_id = str(getattr(result, "installed_id", "") or "")
        self._finish(cleanup=True)
        if installed_id:
            self.import_succeeded.emit(installed_id)

    # ---- session teardown --------------------------------------------------

    def abandon(self) -> None:
        """Invalidate the current session — rebind, window close, or shutdown.

        The token moves on first, so a result already on its way is treated
        as late and cleaned rather than shown. A job still running keeps its
        importer in ``_retired`` for exactly that purpose.
        """
        token = self._token
        self._token += 1
        if not self._active:
            return
        if self._runner.busy and self._importer is not None:
            self._retired[token] = self._importer
        self._prompts.close_review()
        self._finish(cleanup=True)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Stop the import thread and drop staged content before teardown."""
        self.abandon()
        self._runner.shutdown(timeout_ms)
        self._retired.clear()

    def _finish(self, *, cleanup: bool) -> None:
        if cleanup and self._preview is not None:
            _cleanup(self._importer, self._preview)
        self._active = False
        self._phase = ""
        self._source = None
        self._preview = None
        self._importer = None
        self._set_busy(False, "")

    # ---- local messaging ---------------------------------------------------

    def _error(self, message: str) -> None:
        """Import failures are a local dialog and nothing else."""
        self._prompts.show_error(self._dialog_parent(), _ERROR_TITLE, message)

    def _set_busy(self, busy: bool, message: str) -> None:
        busy = bool(busy)
        if busy == self._busy and not busy:
            return
        self._busy = busy
        self.busy_changed.emit(busy, message)


def _cleanup(importer: SkillImporter | None, preview: ImportPreview) -> None:
    """Drop one preview's staging, with or without the importer that made it."""
    if importer is not None:
        importer.cleanup(preview)
        return
    shutil.rmtree(preview.staging_root, ignore_errors=True)


def _local_label(location: str) -> str:
    """The filename or folder name to show, never the path leading to it."""
    cleaned = str(location).replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or cleaned


def _staging_job(importer: SkillImporter, source: ImportSource) -> Callable[[], object]:
    """The blocking acquire-and-validate call this source needs."""
    scope = source.scope
    if source.kind == SOURCE_FOLDER:
        return lambda: importer.preview_from_folder(Path(source.location), destination_scope=scope)
    if source.kind == SOURCE_ZIP:
        return lambda: importer.preview_from_zip(Path(source.location), destination_scope=scope)
    return lambda: importer.preview_from_github(source.location, destination_scope=scope)


def _install_job(
    importer: SkillImporter, preview: ImportPreview, *, replace: bool
) -> Callable[[], object]:
    def install() -> object:
        try:
            return importer.install(preview, replace=replace)
        finally:
            # The worker owns staging once the controller successfully starts
            # this callable, including after rebind, shutdown, or destruction.
            _cleanup(importer, preview)

    return install


__all__ = ["SkillImportController"]
