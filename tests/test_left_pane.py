"""LeftPane structural snapshot and in-place-update tests.

Validates that:
- ``refresh_projects()`` builds project/thread rows on first call.
- Calling ``refresh_projects()`` again with identical project/thread structure
  does *not* clear and recreate rows (in-place update).
- Updating a visible thread title/summary updates the existing ``_ThreadRow``.
- Workspace root change still performs a full rebuild.
- Collapse and show-more structural changes still perform a full rebuild.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from aura.gui.left_pane import (
    LeftPane,
    _ThreadRow,
    _ProjectRow,
    _ShowMoreRow,
    _ElidedLabel,
)
from aura.projects.models import ProjectSpace, ProjectThread


# ── QApplication singleton for the test session ──────────────────────────
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_project(
    pid: str = "p1",
    name: str = "Test Project",
    root: Path | None = None,
) -> ProjectSpace:
    return ProjectSpace(
        id=pid,
        name=name,
        root_path=root or Path("/tmp/test-project"),
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
    )


def _make_thread(
    tid: str = "t1",
    pid: str = "p1",
    title: str = "Test Thread",
    summary: str = "",
    conv_path: str | None = None,
) -> ProjectThread:
    return ProjectThread(
        id=tid,
        project_id=pid,
        title=title,
        conversation_path=Path(conv_path) if conv_path else None,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        summary=summary,
    )


def _project_rows(pane: LeftPane) -> list[_ProjectRow]:
    """Return ordered ``_ProjectRow`` widgets in the left pane layout."""
    rows: list[_ProjectRow] = []
    for i in range(pane._projects_layout.count()):
        item = pane._projects_layout.itemAt(i)
        if item is not None and isinstance(item.widget(), _ProjectRow):
            rows.append(item.widget())
    return rows


def _thread_rows(pane: LeftPane) -> list[_ThreadRow]:
    """Return ordered ``_ThreadRow`` widgets in the left pane layout."""
    rows: list[_ThreadRow] = []
    for i in range(pane._projects_layout.count()):
        item = pane._projects_layout.itemAt(i)
        if item is not None and isinstance(item.widget(), _ThreadRow):
            rows.append(item.widget())
    return rows


def _show_more_rows(pane: LeftPane) -> list[_ShowMoreRow]:
    """Return ``_ShowMoreRow`` widgets in the left pane layout."""
    rows: list[_ShowMoreRow] = []
    for i in range(pane._projects_layout.count()):
        item = pane._projects_layout.itemAt(i)
        if item is not None and isinstance(item.widget(), _ShowMoreRow):
            rows.append(item.widget())
    return rows


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_store():
    """Return a mock ProjectStore that returns a fixed project/thread list."""
    with patch("aura.gui.left_pane.ProjectStore") as MockStore:
        store = MagicMock()
        MockStore.return_value = store
        yield store


@pytest.fixture
def pane(qapp, mock_store):
    """Return a LeftPane with a mock ProjectStore."""
    ws_root = Path("/tmp/test-workspace")
    pane = LeftPane(ws_root)
    yield pane
    # Cleanup
    pane.deleteLater()


# ── Tests: structural snapshot helpers ────────────────────────────────────

class TestVisibleThreadSlice:
    def test_all_visible_when_under_limit(self):
        threads = [_make_thread(f"t{i}") for i in range(5)]
        vt, hm = LeftPane._visible_thread_slice(threads, False, limit=10)
        assert len(vt) == 5
        assert hm is False

    def test_truncated_when_over_limit_and_not_show_all(self):
        threads = [_make_thread(f"t{i}") for i in range(15)]
        vt, hm = LeftPane._visible_thread_slice(threads, False, limit=10)
        assert len(vt) == 10
        assert hm is True

    def test_all_visible_when_show_all(self):
        threads = [_make_thread(f"t{i}") for i in range(15)]
        vt, hm = LeftPane._visible_thread_slice(threads, True, limit=10)
        assert len(vt) == 15
        assert hm is False


class TestSnapshotsEqual:
    def test_identical_snapshots_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1", "t2"]},
            "has_more": {"p1": False},
        }
        new = dict(old)  # shallow copy is fine for these values
        assert LeftPane._snapshots_equal(old, new) is True

    def test_different_workspace_root_not_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1"]},
            "has_more": {"p1": False},
        }
        new = dict(old)
        new["workspace_root"] = Path("/other")
        assert LeftPane._snapshots_equal(old, new) is False

    def test_different_project_ids_not_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1"]},
            "has_more": {"p1": False},
        }
        new = dict(old)
        new["project_ids"] = ["p2"]
        new["active_project_id"] = "p2"
        new["visible_thread_ids"] = {"p2": ["t1"]}
        new["has_more"] = {"p2": False}
        assert LeftPane._snapshots_equal(old, new) is False

    def test_different_thread_ids_not_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1", "t2"]},
            "has_more": {"p1": False},
        }
        new = dict(old)
        new["visible_thread_ids"] = {"p1": ["t1", "t3"]}
        assert LeftPane._snapshots_equal(old, new) is False

    def test_different_collapsed_not_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1"]},
            "has_more": {"p1": False},
        }
        new = dict(old)
        new["collapsed"] = {"p1": True}
        new["visible_thread_ids"] = {"p1": []}
        assert LeftPane._snapshots_equal(old, new) is False

    def test_different_show_all_not_equal(self):
        old = {
            "workspace_root": Path("/ws"),
            "project_ids": ["p1"],
            "active_project_id": "p1",
            "collapsed": {"p1": False},
            "show_all": False,
            "visible_thread_ids": {"p1": ["t1"]},
            "has_more": {"p1": False},
        }
        new = dict(old)
        new["show_all"] = True
        assert LeftPane._snapshots_equal(old, new) is False


# ── Tests: _ThreadRow.update_thread ───────────────────────────────────────

class TestThreadRowUpdate:
    def test_update_thread_changes_label_and_tooltip(self, qapp):
        t1 = _make_thread("t1", title="Original Title", summary="Original summary")
        row = _ThreadRow(t1)

        assert row.title_label.full_text() == "Original Title"
        assert row.title_label.toolTip() == "Original summary"

        t2 = _make_thread("t2", title="Updated Title", summary="Updated summary")
        row.update_thread(t2)

        assert row.thread is t2
        assert row.title_label.full_text() == "Updated Title"
        assert row.title_label.toolTip() == "Updated summary"

    def test_update_thread_uses_title_as_tooltip_when_no_summary(self, qapp):
        t1 = _make_thread("t1", title="First", summary="Has summary")
        row = _ThreadRow(t1)
        assert row.title_label.toolTip() == "Has summary"

        t2 = _make_thread("t2", title="Second", summary="")
        row.update_thread(t2)
        assert row.title_label.toolTip() == "Second"


# ── Tests: LeftPane.refresh_projects full-rebuild vs in-place ─────────────

class TestRefreshProjectsInitial:
    def test_initial_call_builds_project_rows(self, pane, mock_store):
        """First call to refresh_projects builds project rows from store data."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "My Project", root)
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = []
        mock_store.load_project.return_value = project

        pane.refresh_projects(root)

        prows = _project_rows(pane)
        assert len(prows) == 1
        assert prows[0].project.id == "p1"
        assert prows[0].is_active is True

    def test_initial_call_builds_thread_rows(self, pane, mock_store):
        """Threads for the active project are rendered as _ThreadRow widgets."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "My Project", root)
        threads = [
            _make_thread("t1", "p1", "Thread One"),
            _make_thread("t2", "p1", "Thread Two"),
        ]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        pane.refresh_projects(root)

        trows = _thread_rows(pane)
        assert len(trows) == 2
        assert trows[0].thread.id == "t1"
        assert trows[1].thread.id == "t2"


class TestRefreshProjectsInPlace:
    def test_identical_structure_does_not_recreate_widgets(self, pane, mock_store):
        """Second call with identical structure reuses existing _ThreadRow widgets."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        threads = [
            _make_thread("t1", "p1", "First Thread", "summary 1"),
            _make_thread("t2", "p1", "Second Thread", "summary 2"),
        ]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        # First call — build
        pane.refresh_projects(root)
        trows_before = _thread_rows(pane)
        assert len(trows_before) == 2
        row_t1_before = trows_before[0]
        row_t2_before = trows_before[1]

        # Second call — same structure, only metadata changed
        threads_updated = [
            _make_thread("t1", "p1", "First Thread Updated", "summary 1 updated"),
            _make_thread("t2", "p1", "Second Thread Updated", "summary 2 updated"),
        ]
        mock_store.list_threads.return_value = threads_updated

        pane.refresh_projects(root)
        trows_after = _thread_rows(pane)
        assert len(trows_after) == 2

        # Same widget objects (not recreated)
        assert trows_after[0] is row_t1_before
        assert trows_after[1] is row_t2_before

        # Labels updated in place
        assert trows_after[0].title_label.full_text() == "First Thread Updated"
        assert trows_after[0].title_label.toolTip() == "summary 1 updated"
        assert trows_after[1].title_label.full_text() == "Second Thread Updated"
        assert trows_after[1].title_label.toolTip() == "summary 2 updated"

    def test_thread_title_update_without_summary(self, pane, mock_store):
        """Title-only update (no summary) is applied in place."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        threads = [_make_thread("t1", "p1", "Original", "")]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        pane.refresh_projects(root)
        row_before = _thread_rows(pane)[0]
        assert row_before.title_label.full_text() == "Original"
        assert row_before.title_label.toolTip() == "Original"  # fallback

        # Update with new title, still no summary
        threads_updated = [_make_thread("t1", "p1", "Renamed", "")]
        mock_store.list_threads.return_value = threads_updated

        pane.refresh_projects(root)
        row_after = _thread_rows(pane)[0]
        assert row_after is row_before
        assert row_after.title_label.full_text() == "Renamed"
        assert row_after.title_label.toolTip() == "Renamed"

    def test_active_project_not_last_still_updates_in_place(self, pane, mock_store):
        """Multiple projects with active first — snapshot detects correct active
        project via ``row.is_active``, not position."""
        root = Path("/tmp/test-workspace")
        p1 = _make_project("p1", "Active Project", root)
        p2 = _make_project("p2", "Inactive Project", Path("/tmp/other"))
        threads_p1 = [
            _make_thread("t1", "p1", "Active Thread 1", "s1"),
            _make_thread("t2", "p1", "Active Thread 2", "s2"),
        ]
        threads_p2: list[ProjectThread] = []

        mock_store.list_projects.return_value = [p1, p2]
        mock_store.load_project.side_effect = lambda pid: {  # type: ignore[attr-defined]
            "p1": p1,
            "p2": p2,
        }.get(pid)

        # list_threads depends on which project
        def _list_threads(proj, **kw):
            if proj.id == "p1":
                return list(threads_p1)
            return list(threads_p2)

        mock_store.list_threads.side_effect = _list_threads

        # First call — build
        pane.refresh_projects(root)
        prows = _project_rows(pane)
        assert len(prows) == 2
        assert prows[0].is_active is True   # p1 is active, first in layout
        assert prows[1].is_active is False  # p2 is inactive

        trows_before = _thread_rows(pane)
        assert len(trows_before) == 2
        row_t1_before = trows_before[0]
        row_t2_before = trows_before[1]

        # Second call — same structure, only thread metadata changed
        threads_p1_updated = [
            _make_thread("t1", "p1", "Active Thread 1 Updated", "s1 updated"),
            _make_thread("t2", "p1", "Active Thread 2 Updated", "s2 updated"),
        ]
        # Update the side_effect closure
        def _list_threads_v2(proj, **kw):
            if proj.id == "p1":
                return list(threads_p1_updated)
            return list(threads_p2)

        mock_store.list_threads.side_effect = _list_threads_v2

        pane.refresh_projects(root)
        trows_after = _thread_rows(pane)
        assert len(trows_after) == 2

        # Same widget objects — not recreated
        assert trows_after[0] is row_t1_before
        assert trows_after[1] is row_t2_before

        # Labels updated in place
        assert trows_after[0].title_label.full_text() == "Active Thread 1 Updated"
        assert trows_after[0].title_label.toolTip() == "s1 updated"
        assert trows_after[1].title_label.full_text() == "Active Thread 2 Updated"
        assert trows_after[1].title_label.toolTip() == "s2 updated"

        # Snapshot captures active correctly from is_active, not position
        snap = pane._capture_layout_snapshot()
        assert snap["active_project_id"] == "p1"
        assert snap["project_ids"] == ["p1", "p2"]


class TestRefreshProjectsRebuild:
    def test_workspace_root_change_rebuilds(self, pane, mock_store):
        """Changing workspace root triggers a full rebuild."""
        root1 = Path("/tmp/ws1")
        root2 = Path("/tmp/ws2")

        project1 = _make_project("p1", "Project 1", root1)
        project2 = _make_project("p2", "Project 2", root2)

        mock_store.list_projects.side_effect = [[project1], [project2]]
        mock_store.list_threads.return_value = []
        mock_store.load_project.return_value = project1

        # First call
        pane.refresh_projects(root1)
        prows1 = _project_rows(pane)
        assert len(prows1) == 1
        row_before = prows1[0]

        # Second call with different root
        mock_store.load_project.return_value = project2
        mock_store.list_projects.side_effect = None
        mock_store.list_projects.return_value = [project2]

        pane.refresh_projects(root2)
        prows2 = _project_rows(pane)
        assert len(prows2) == 1
        # Widgets are new (not the same object)
        assert prows2[0] is not row_before
        assert prows2[0].project.id == "p2"

    def test_collapse_toggle_rebuilds(self, pane, mock_store):
        """Collapsing a project changes structure → full rebuild."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        threads = [_make_thread("t1", "p1", "Thread")]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        # Build expanded
        pane.refresh_projects(root)
        trows_before = _thread_rows(pane)
        assert len(trows_before) == 1

        # Collapse
        pane._project_collapsed["p1"] = True
        pane.refresh_projects(root)
        trows_after = _thread_rows(pane)
        # Thread rows are gone (collapsed)
        assert len(trows_after) == 0

    def test_show_more_changes_rebuilds(self, pane, mock_store):
        """Toggling 'show more' changes visible thread count → full rebuild."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        threads = [_make_thread(f"t{i}", "p1", f"Thread {i}") for i in range(15)]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        # First call: only 10 visible, has "Show more"
        pane.refresh_projects(root)
        trows_before = _thread_rows(pane)
        assert len(trows_before) == 10
        assert len(_show_more_rows(pane)) == 1

        # Toggle show all
        pane._show_all_active_threads = True
        pane.refresh_projects(root)
        trows_after = _thread_rows(pane)
        assert len(trows_after) == 15
        assert len(_show_more_rows(pane)) == 0


# ── Tests: capture_layout_snapshot ────────────────────────────────────────

class TestCaptureLayoutSnapshot:
    def test_empty_layout_returns_empty_snapshot(self, pane):
        snap = pane._capture_layout_snapshot()
        assert snap["project_ids"] == []
        assert snap["active_project_id"] is None

    def test_snapshot_captures_project_and_thread_ids(self, pane, mock_store):
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        threads = [
            _make_thread("t1", "p1", "Thread 1"),
            _make_thread("t2", "p1", "Thread 2"),
        ]
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = threads
        mock_store.load_project.return_value = project

        pane.refresh_projects(root)

        snap = pane._capture_layout_snapshot()
        assert snap["workspace_root"] == root
        assert snap["project_ids"] == ["p1"]
        assert snap["active_project_id"] == "p1"
        assert snap["collapsed"] == {"p1": False}
        assert snap["visible_thread_ids"] == {"p1": ["t1", "t2"]}
        assert snap["has_more"] == {"p1": False}
        assert snap["show_all"] is False


# ── Tests: model config controls survive rebuild ──────────────────────────

class TestModelConfigPersistence:
    def test_model_combos_preserved_after_refresh(self, pane, mock_store):
        """Model config controls (footer) are untouched by refresh_projects."""
        from aura.config import ProviderId

        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = []
        mock_store.load_project.return_value = project

        # Populate models
        pane.populate_models(ProviderId("openai"), ProviderId("openai"))

        # Set specific values
        planner_combo = pane._planner_model_combo
        if planner_combo.count() > 0:
            planner_combo.setCurrentIndex(min(1, planner_combo.count() - 1))
            planner_model_before = pane.current_planner_model()

            pane.refresh_projects(root)

            # Model combo should be unchanged after refresh
            assert pane.current_planner_model() == planner_model_before

    def test_thinking_combo_preserved_after_refresh(self, pane, mock_store):
        """Thinking mode dropdowns are untouched by refresh_projects."""
        root = Path("/tmp/test-workspace")
        project = _make_project("p1", "Test", root)
        mock_store.list_projects.return_value = [project]
        mock_store.list_threads.return_value = []
        mock_store.load_project.return_value = project

        pane.set_planner_thinking("max")
        pane.set_worker_thinking("off")

        pane.refresh_projects(root)

        assert pane.current_planner_thinking() == "max"
        assert pane.current_worker_thinking() == "off"
