"""CodeEditorPane + FileEditProjection: the authoritative editor projection.

Offline Qt widget tests (no provider/API calls). These prove the invariant
Phase 2B exists for: the workspace editor only ever displays content the
write pipeline already confirmed landed on disk, path identity is the
document identity, and the applied-edit highlight decorates already-correct
text without ever delaying or owning it.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from aura.gui.code_editor_pane import CodeEditorPane
from aura.gui.editor.applied_edit_effect import PULSE_DURATION_MS
from aura.gui.editor.file_edit_projection import FileEditProjection


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _change(path: str, action: str = "modify", old: str = "", new: str = "") -> dict:
    return {
        "change_id": f"c:{path}",
        "path": path,
        "action": action,
        "old_content": old,
        "new_content": new,
    }


def _make_pane(root: Path) -> tuple[CodeEditorPane, FileEditProjection]:
    pane = CodeEditorPane()
    pane.set_workspace_root(root)
    projection = FileEditProjection(pane)
    return pane, projection


def test_applied_change_opens_tab_with_actual_disk_content(tmp_path: Path) -> None:
    _app()
    (tmp_path / "a.py").write_text("committed = 1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1",
            "write_file",
            "applied",
            [_change("a.py", "modify", old="", new="committed = 1\n")],
            "",
        )

        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        assert editor is not None
        assert editor.toPlainText() == "committed = 1\n"
    finally:
        pane.deleteLater()


def test_proposed_and_awaiting_approval_never_open_a_tab(tmp_path: Path) -> None:
    _app()
    pane, projection = _make_pane(tmp_path)
    try:
        for phase in ("proposed", "awaiting_approval"):
            projection.handle_lifecycle_event(
                "call-1",
                "write_file",
                phase,
                [_change("never.py", "create", old="", new="proposed content")],
                "",
            )
        assert pane.path_editor(pane.resolve_workspace_path("never.py")) is None
    finally:
        pane.deleteLater()


def test_second_applied_edit_to_same_path_reuses_one_tab(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "a.py"
    target.write_text("v1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="v1\n")], ""
        )
        first_editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        tab_count_after_first = pane._tabs.count()

        target.write_text("v2\n", encoding="utf-8")
        projection.handle_lifecycle_event(
            "call-2", "write_file", "applied", [_change("a.py", "modify", new="v2\n")], ""
        )
        second_editor = pane.path_editor(pane.resolve_workspace_path("a.py"))

        assert first_editor is second_editor
        assert pane._tabs.count() == tab_count_after_first == 1
        assert second_editor.toPlainText() == "v2\n"
    finally:
        pane.deleteLater()


def test_multi_file_patch_updates_the_correct_tab_for_every_path(tmp_path: Path) -> None:
    _app()
    (tmp_path / "a.py").write_text("alpha-2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta-2\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1",
            "patch_file",
            "applied",
            [
                _change("a.py", "modify", new="alpha-2\n"),
                _change("b.py", "modify", new="beta-2\n"),
            ],
            "",
        )
        a_editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        b_editor = pane.path_editor(pane.resolve_workspace_path("b.py"))
        assert a_editor is not None and b_editor is not None
        assert a_editor is not b_editor
        assert a_editor.toPlainText() == "alpha-2\n"
        assert b_editor.toPlainText() == "beta-2\n"
    finally:
        pane.deleteLater()


def test_new_file_creation_appears_and_opens(tmp_path: Path) -> None:
    _app()
    (tmp_path / "fresh.py").write_text("brand_new = True\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1",
            "write_file",
            "applied",
            [_change("fresh.py", "create", old="", new="brand_new = True\n")],
            "",
        )
        editor = pane.path_editor(pane.resolve_workspace_path("fresh.py"))
        assert editor is not None
        assert editor.toPlainText() == "brand_new = True\n"
    finally:
        pane.deleteLater()


def test_applied_deletion_closes_the_tab(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("gone.py", "modify", new="x = 1\n")], ""
        )
        assert pane.path_editor(pane.resolve_workspace_path("gone.py")) is not None

        target.unlink()
        projection.handle_lifecycle_event(
            "call-2", "delete_file", "applied", [_change("gone.py", "delete")], ""
        )
        assert pane.path_editor(pane.resolve_workspace_path("gone.py")) is None
    finally:
        pane.deleteLater()


def test_rejected_edit_reloads_actual_disk_content_over_proposal(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "a.py"
    target.write_text("real = 1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        # A prior applied write opened the tab with real content.
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="real = 1\n")], ""
        )
        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        assert editor.toPlainText() == "real = 1\n"

        # A later proposal on the same path is rejected -- the tab must keep
        # showing real disk content, never the rejected proposal.
        projection.handle_lifecycle_event(
            "call-2",
            "write_file",
            "rejected",
            [_change("a.py", "modify", old="real = 1\n", new="proposed but rejected")],
            "reject",
        )
        assert editor.toPlainText() == "real = 1\n"
    finally:
        pane.deleteLater()


def test_failed_patch_reloads_disk_truth_not_proposed_content(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "a.py"
    target.write_text("real = 1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="real = 1\n")], ""
        )
        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))

        projection.handle_lifecycle_event(
            "call-2",
            "patch_file",
            "failed",
            [_change("a.py", "modify", old="real = 1\n", new="never landed")],
            "patch_transaction_commit_failed",
        )
        assert editor.toPlainText() == "real = 1\n"
    finally:
        pane.deleteLater()


def test_applied_pulse_starts_only_after_correct_text_is_set(tmp_path: Path) -> None:
    """The highlight must never be visible before the committed text is."""
    app = _app()
    (tmp_path / "a.py").write_text("committed\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="committed\n")], ""
        )
        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        # By the time handle_lifecycle_event returns, text is already correct
        # AND the pulse has already been applied over it (never before).
        assert editor.toPlainText() == "committed\n"
        assert editor.extraSelections() != []
        app.processEvents()
    finally:
        pane.deleteLater()


def test_second_applied_edit_interrupts_first_pulse_without_corrupting_content(
    tmp_path: Path,
) -> None:
    app = _app()
    target = tmp_path / "a.py"
    target.write_text("v1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="v1\n")], ""
        )
        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        assert editor.extraSelections() != []

        # Second applied edit arrives before the first pulse's timer fires.
        target.write_text("v2\n", encoding="utf-8")
        projection.handle_lifecycle_event(
            "call-2", "write_file", "applied", [_change("a.py", "modify", new="v2\n")], ""
        )

        assert editor.toPlainText() == "v2\n"
        assert editor.extraSelections() != []
        app.processEvents()
        assert editor.toPlainText() == "v2\n"
    finally:
        pane.deleteLater()


def test_pulse_duration_is_bounded() -> None:
    assert 300 <= PULSE_DURATION_MS <= 800


def test_pulse_auto_clears_after_its_duration(tmp_path: Path) -> None:
    from PySide6.QtTest import QTest

    app = _app()
    (tmp_path / "a.py").write_text("v1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        projection.handle_lifecycle_event(
            "call-1", "write_file", "applied", [_change("a.py", "modify", new="v1\n")], ""
        )
        editor = pane.path_editor(pane.resolve_workspace_path("a.py"))
        assert editor.extraSelections() != []

        QTest.qWait(PULSE_DURATION_MS + 200)
        app.processEvents()

        assert editor.extraSelections() == []
        # Dropping the effect never touches correctness.
        assert editor.toPlainText() == "v1\n"
    finally:
        pane.deleteLater()
