"""Own one plain-language skill-creation session and its workspace draft."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from aura.gui.skills_manager.creation_dialogs import SkillCreationPrompts, SkillCreationRequest
from aura.gui.skills_manager.import_controller import SkillImportController
from aura.paths import first_link_like_component, is_link_like

logger = logging.getLogger(__name__)

_NO_WORKSPACE = "Open a project first — Aura needs a workspace in which to create the draft."
_ALREADY_RUNNING = "Aura is already creating a skill. Finish or cancel that creation first."
_START_FAILED = "Aura could not start the creation turn. No draft was kept."
_MISSING_DRAFT = "Aura finished without creating exactly one skill draft containing SKILL.md."
_INVALID_DESCRIPTION = "Describe what Aura should become better at before creating the skill."
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


class DraftLease:
    """Idempotent, link-safe cleanup authority for one exact draft root."""

    def __init__(self, workspace: Path, session_id: str) -> None:
        self.workspace = Path(workspace)
        self.session_id = session_id
        self.root = self.workspace / ".aura" / "skills" / "drafts" / session_id
        self._lock = threading.Lock()

    def release(self) -> None:
        """Remove only this session root, never resolving through a link."""
        with self._lock:
            try:
                if not _is_exact_safe_draft(self.workspace, self.root, self.session_id):
                    logger.warning("skills creation: refused unsafe draft cleanup")
                    return
                if not os.path.lexists(self.root):
                    return
                relative = self.root.relative_to(self.workspace)
                if first_link_like_component(self.workspace, relative.parts) is not None:
                    logger.warning("skills creation: refused linked draft cleanup")
                    return
                shutil.rmtree(self.root)
            except Exception:
                logger.debug("skills creation: draft cleanup failed", exc_info=True)


class SkillCreationController(QObject):
    """Intake → draft → exact Aura turn → generated-folder handoff → cleanup."""

    active_changed = Signal(bool)
    busy_changed = Signal(bool, str)

    def __init__(
        self,
        *,
        import_controller: SkillImportController,
        start_turn: Callable[[str, str], bool] | None,
        prompts: SkillCreationPrompts | None = None,
        dialog_parent: Callable[[], QWidget | None] | None = None,
        workspace_root: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._imports = import_controller
        self._start_turn = start_turn
        self._prompts = prompts if prompts is not None else SkillCreationPrompts()
        self._dialog_parent = dialog_parent or (lambda: None)
        self._workspace_root = _resolved_workspace(workspace_root)
        self._active = False
        self._busy = False
        self._turn_id = ""
        self._session_id = ""
        self._request: SkillCreationRequest | None = None
        self._lease: DraftLease | None = None
        self._retired: dict[str, DraftLease] = {}

        self._imports.generated_source_acquired.connect(self._on_source_acquired)
        self._imports.generated_import_finished.connect(self._on_import_finished)

    def is_active(self) -> bool:
        return self._active

    def set_workspace_root(self, root: Path | None) -> None:
        self.abandon()
        self._workspace_root = _resolved_workspace(root)

    def start(self) -> bool:
        """Collect intake and launch one ordinary visible production turn."""
        if self._active:
            self._error(_ALREADY_RUNNING)
            return False
        workspace = self._workspace_root
        if workspace is None:
            self._error(_NO_WORKSPACE)
            return False

        self._active = True
        self.active_changed.emit(True)
        request = self._prompts.ask(self._dialog_parent())
        if request is None:
            self._finish()
            return False
        if not request.description.strip():
            self._finish()
            self._error(_INVALID_DESCRIPTION)
            return False

        session_id = uuid.uuid4().hex
        lease = DraftLease(workspace, session_id)
        try:
            _allocate_draft(lease)
        except Exception:
            logger.debug("skills creation: draft allocation failed", exc_info=True)
            lease.release()
            self._finish()
            self._error(_START_FAILED)
            return False

        self._session_id = session_id
        self._turn_id = session_id
        self._request = request
        self._lease = lease
        prompt = build_creation_prompt(request, lease.root.relative_to(workspace))
        started = (
            self._start_turn(prompt, session_id)
            if self._start_turn is not None
            else False
        )
        if not started:
            lease.release()
            self._finish()
            self._error(_START_FAILED)
            return False

        self._set_busy(True, "Aura is creating a skill draft…")
        return True

    def turn_finished(self, turn_id: str, *, successful: bool) -> None:
        """Accept only the exact production turn this session launched."""
        turn_id = str(turn_id)
        if turn_id != self._turn_id or not self._active:
            lease = self._retired.pop(turn_id, None)
            if lease is not None:
                lease.release()
            return
        if not successful:
            self._release_active_draft()
            self._finish()
            return

        lease = self._lease
        request = self._request
        if lease is None or request is None:
            self._finish()
            return
        candidate = _one_candidate_root(lease.root)
        if candidate is None:
            self._release_active_draft()
            self._finish()
            self._error(_MISSING_DRAFT)
            return

        self._set_busy(True, "Preparing Aura’s skill for review…")
        started = self._imports.start_generated_folder(
            candidate,
            scope=request.scope,
            owner_id=self._session_id,
            release_source=lease.release,
        )
        if not started:
            self._release_active_draft()
            self._finish()

    def abandon(self) -> None:
        """Invalidate UI ownership while retaining any worker-held draft lease."""
        if not self._active:
            return
        turn_id = self._turn_id
        lease = self._lease
        # During generated preview, the importer worker owns release timing.
        # Before handoff, retain a lease so a stopped worker that recreates its
        # assigned folder is cleaned again at the exact turn's late finish.
        if self._imports.is_generated_owner(self._session_id):
            pass
        else:
            if turn_id and lease is not None:
                self._retired[turn_id] = lease
            self._release_active_draft()
        self._finish()

    def shutdown(self) -> None:
        self.abandon()

    def _on_source_acquired(self, owner_id: str) -> None:
        if owner_id == self._session_id:
            self._lease = None

    def _on_import_finished(self, owner_id: str) -> None:
        if owner_id != self._session_id or not self._active:
            return
        self._finish()

    def _release_active_draft(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.release()

    def _finish(self) -> None:
        was_active = self._active
        self._active = False
        self._turn_id = ""
        self._session_id = ""
        self._request = None
        self._lease = None
        self._set_busy(False, "")
        if was_active:
            self.active_changed.emit(False)

    def _error(self, message: str) -> None:
        self._prompts.show_error(self._dialog_parent(), message)

    def _set_busy(self, busy: bool, message: str) -> None:
        busy = bool(busy)
        if busy == self._busy and not busy:
            return
        self._busy = busy
        self.busy_changed.emit(busy, message)


def build_creation_prompt(request: SkillCreationRequest, draft_relative: Path) -> str:
    """Give the normal Aura turn a precise, workspace-relative authoring contract."""
    preferred = request.preferred_name.strip()
    name_line = (
        f"Use this preferred name when it can be normalized appropriately: {preferred!r}."
        if preferred
        else "Choose the skill name yourself."
    )
    target = draft_relative.as_posix()
    return f"""Create one Aura skill for this request:

{request.description.strip()}

Inspect the current project when that would make the skill more accurate.
Create exactly one valid skill inside `{target}/` and nowhere else. {name_line}
The skill must have a `SKILL.md` with a valid lowercase kebab-case name, a concise description, and useful instructions.
Include only relevant optional metadata from: task_kinds, path_globs, triggers, workspace_markers, model.
Create supporting text resources only when they materially improve the skill.
Use Aura's normal workspace-relative `apply_patch` path and normal diff approval.
Make no unrelated workspace changes. Do not write into Project or Personal installed-skill directories.
Do not install, commit, push, execute, or test scripts from the generated skill.
Finish after authoring the draft; the user will review and explicitly decide whether to install it.
"""


def _resolved_workspace(root: Path | None) -> Path | None:
    if root is None:
        return None
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_dir() and not is_link_like(resolved) else None


def _is_exact_safe_draft(workspace: Path, root: Path, session_id: str) -> bool:
    if not _SESSION_RE.fullmatch(session_id):
        return False
    try:
        workspace_abs = Path(os.path.abspath(workspace))
        root_abs = Path(os.path.abspath(root))
    except (OSError, ValueError):
        return False
    expected = workspace_abs / ".aura" / "skills" / "drafts" / session_id
    return root_abs == expected and root_abs != workspace_abs


def _allocate_draft(lease: DraftLease) -> None:
    if not _is_exact_safe_draft(lease.workspace, lease.root, lease.session_id):
        raise ValueError("unsafe draft path")
    relative_parent = lease.root.parent.relative_to(lease.workspace)
    linked = first_link_like_component(lease.workspace, relative_parent.parts)
    if linked is not None:
        raise ValueError("draft parent is linked")
    lease.root.mkdir(parents=True, exist_ok=False)
    if is_link_like(lease.root):
        raise ValueError("draft root is linked")


def _one_candidate_root(draft_root: Path) -> Path | None:
    """Return the only real folder containing SKILL.md, without following links."""
    if not draft_root.is_dir() or is_link_like(draft_root):
        return None
    candidates: list[Path] = []
    try:
        for root, dirnames, filenames in os.walk(draft_root, followlinks=False):
            root_path = Path(root)
            for dirname in tuple(dirnames):
                if is_link_like(root_path / dirname):
                    dirnames.remove(dirname)
            if "SKILL.md" in filenames:
                manifest = root_path / "SKILL.md"
                if not is_link_like(manifest) and manifest.is_file():
                    candidates.append(root_path)
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


__all__ = ["DraftLease", "SkillCreationController", "build_creation_prompt"]
