"""Projects the authoritative file-edit lifecycle into workspace editor tabs.

Binds the file-edit lifecycle (``proposed`` -> ``awaiting_approval`` ->
``applied`` | ``rejected`` | ``failed``) to ``CodeEditorPane``'s path-keyed
tabs. Only an ``applied`` phase is ever allowed to change what a tab displays
as committed; ``rejected``/``failed`` reload whatever is truthfully on disk so
a previously-open tab never keeps showing a proposal that never landed.
``proposed``/``awaiting_approval`` are intentionally inert here -- that stage
belongs to the approval dialog and the diff card, never to this editor.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aura.gui.code_editor_pane import CodeEditorPane

logger = logging.getLogger(__name__)

_RELOAD_DISK_TRUTH_PHASES = frozenset({"rejected", "failed"})


class FileEditProjection:
    """Routes ``FileEditLifecycle`` phase events into the workspace editor."""

    def __init__(self, pane: "CodeEditorPane") -> None:
        self._pane = pane

    def handle_lifecycle_event(
        self,
        tool_call_id: str,
        tool_name: str,
        phase: str,
        changes: list[dict],
        reason: str,
    ) -> None:
        if phase == "applied":
            for change in changes:
                self._apply(change)
        elif phase in _RELOAD_DISK_TRUTH_PHASES:
            for change in changes:
                self._reload_disk_truth(change)

    # ------------------------------------------------------------------

    def _apply(self, change: dict) -> None:
        raw_path = str(change.get("path") or "")
        if not raw_path:
            return
        resolved = self._pane.resolve_workspace_path(raw_path)
        if change.get("action") == "delete":
            self._pane.close_path(resolved)
            return

        content = self._pane.read_disk_text(resolved)
        if content is None:
            logger.warning(
                "file_edit_projection_applied_unreadable path=%s", resolved
            )
            return
        old_text = str(change.get("old_content") or "")
        editor = self._pane.set_path_content(
            resolved, content, mark_execution_origin=True
        )
        self._pane.pulse_applied(editor, old_text, content)

    def _reload_disk_truth(self, change: dict) -> None:
        raw_path = str(change.get("path") or "")
        if not raw_path:
            return
        resolved = self._pane.resolve_workspace_path(raw_path)
        editor = self._pane.path_editor(resolved)
        if editor is None:
            # Nothing was ever shown as committed for this path here -- no
            # stale projection to correct.
            return
        content = self._pane.read_disk_text(resolved)
        if content is not None:
            editor.setPlainText(content)
        else:
            # The file does not exist on disk (e.g. a rejected create that
            # never landed) -- close the tab rather than show empty content
            # as if it were real.
            self._pane.close_path(resolved)


__all__ = ["FileEditProjection"]
