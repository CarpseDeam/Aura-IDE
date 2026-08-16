"""Phase 2C: the canonical ``shell`` tool and post-command workspace reconciliation.

Offline, no provider/API calls. Covers:

* ``shell`` is the tool-result name a submitted command reports under
  (replacing ``run_terminal_command``).
* A submitted command -- whether it exits zero, nonzero, times out, or is
  cancelled -- always requests one ``WorkspaceReconcileRequested`` event; a
  command that never actually submitted (a pre-execution validation error)
  requests none.
* ``ExecutionEventRelay`` forwards that event as ``workspaceReconcileRequested``.
* ``FileEditProjection.reconcile_workspace()`` re-reads every open tab from
  disk, replaces stale content, closes tabs for files that no longer exist,
  and never fabricates a pulse/applied claim for an opaque shell command.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import ToolResult, WorkspaceReconcileRequested
from aura.conversation.history import History
from aura.conversation.shell_tool import ShellTool
from aura.events import EventBus
from aura.gui.code_editor_pane import CodeEditorPane
from aura.gui.editor.file_edit_projection import FileEditProjection
from aura.shell.powershell_session import PowerShellCommandResult


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _FakeSession:
    """Stands in for PowerShellSession: returns a canned result, no subprocess."""

    def __init__(self, result: PowerShellCommandResult) -> None:
        self._result = result
        self.current_cwd = Path(".").resolve()
        self.session_id = "fake-session"
        self.closed = False

    def execute(self, command, *, timeout, cancel_event, on_output, on_submitted):
        if self._result.submitted and on_submitted is not None:
            on_submitted()
        return self._result

    def close(self) -> None:
        self.closed = True


def _make_shell_tool(result: PowerShellCommandResult) -> tuple[ShellTool, History]:
    history = History()
    fake_session = _FakeSession(result)
    tool = ShellTool(history, Path("."), session_factory=lambda _root: fake_session)
    return tool, history


def _run(tool: ShellTool, command: str = "echo hi") -> list[object]:
    import threading

    events: list[object] = []
    tool.handle_terminal_command(
        tool_call_id="call-1",
        args={"command": command},
        on_event=events.append,
        cancel_event=threading.Event(),
    )
    return events


def _reconcile_events(events: list[object]) -> list[WorkspaceReconcileRequested]:
    return [e for e in events if isinstance(e, WorkspaceReconcileRequested)]


def _tool_results(events: list[object]) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


# ── shell replaces the old result/event name everywhere ─────────────────────


def test_shell_tool_result_is_named_shell() -> None:
    result = PowerShellCommandResult(
        ok=True, stdout="hi", stderr="", output="hi", exit_code=0,
        cwd=str(Path(".").resolve()), submitted=True,
    )
    tool, _history = _make_shell_tool(result)
    events = _run(tool)

    results = _tool_results(events)
    assert len(results) == 1
    assert results[0].name == "shell"


# ── reconciliation fires for every submitted outcome ────────────────────────


def test_reconciliation_requested_after_clean_exit() -> None:
    result = PowerShellCommandResult(
        ok=True, stdout="", stderr="", output="", exit_code=0,
        cwd=str(Path(".").resolve()), submitted=True,
    )
    tool, _history = _make_shell_tool(result)
    events = _run(tool)

    assert len(_reconcile_events(events)) == 1
    assert _reconcile_events(events)[0].tool_call_id == "call-1"


def test_reconciliation_requested_after_nonzero_exit() -> None:
    result = PowerShellCommandResult(
        ok=False, stdout="", stderr="boom", output="boom", exit_code=1,
        cwd=str(Path(".").resolve()), submitted=True,
    )
    tool, _history = _make_shell_tool(result)
    events = _run(tool)

    assert len(_reconcile_events(events)) == 1


def test_reconciliation_requested_after_timeout() -> None:
    result = PowerShellCommandResult(
        ok=False, stdout="", stderr="", output="partial", exit_code=None,
        cwd=str(Path(".").resolve()), submitted=True, timed_out=True,
    )
    tool, _history = _make_shell_tool(result)
    events = _run(tool)

    assert len(_reconcile_events(events)) == 1


def test_reconciliation_requested_after_cancellation() -> None:
    result = PowerShellCommandResult(
        ok=False, stdout="", stderr="", output="partial", exit_code=None,
        cwd=str(Path(".").resolve()), submitted=True, cancelled=True,
    )
    tool, _history = _make_shell_tool(result)
    events = _run(tool)

    assert len(_reconcile_events(events)) == 1


def test_no_reconciliation_when_command_never_submitted() -> None:
    """A pre-execution validation error (bad cwd) never reaches the session,
    so nothing in the workspace could have changed -- no reconciliation."""
    tool, _history = _make_shell_tool(
        PowerShellCommandResult(
            ok=True, stdout="", stderr="", output="", exit_code=0, cwd=".",
        )
    )
    import threading

    events: list[object] = []
    tool.handle_terminal_command(
        tool_call_id="call-1",
        args={"command": "echo hi", "cwd": "../outside"},
        on_event=events.append,
        cancel_event=threading.Event(),
    )

    assert _reconcile_events(events) == []


# ── relay forwards the reconciliation event ──────────────────────────────────


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def test_relay_forwards_workspace_reconcile_requested() -> None:
    relay = ExecutionEventRelay(_ApprovalProxy(), EventBus())
    received: list[tuple[str, str]] = []
    relay.workspaceReconcileRequested.connect(lambda *args: received.append(args))

    relay.relay("run-1", WorkspaceReconcileRequested(tool_call_id="call-9"))

    assert received == [("run-1", "call-9")]


# ── GUI reconciliation: re-sync open tabs from disk, no fake applied claim ──


def _make_pane(root: Path) -> tuple[CodeEditorPane, FileEditProjection]:
    pane = CodeEditorPane()
    pane.set_workspace_root(root)
    return pane, FileEditProjection(pane)


def test_reconcile_workspace_rereads_stale_open_tab_from_disk(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "generated.py"
    target.write_text("v1\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        pane.open_file(target)
        editor = pane.path_editor(pane.resolve_workspace_path("generated.py"))
        assert editor.toPlainText() == "v1\n"

        # A shell command (formatter/generator) rewrote the file outside the
        # file-edit lifecycle.
        target.write_text("v2 -- reformatted\n", encoding="utf-8")
        projection.reconcile_workspace()

        assert editor.toPlainText() == "v2 -- reformatted\n"
        # Reconciliation makes no per-file "applied" claim: no pulse fires.
        assert editor.extraSelections() == []
    finally:
        pane.deleteLater()


def test_reconcile_workspace_closes_tab_for_a_file_shell_deleted(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "temp.py"
    target.write_text("scratch\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        pane.open_file(target)
        assert pane.path_editor(pane.resolve_workspace_path("temp.py")) is not None

        target.unlink()
        projection.reconcile_workspace()

        assert pane.path_editor(pane.resolve_workspace_path("temp.py")) is None
    finally:
        pane.deleteLater()


def test_reconcile_workspace_reveals_no_new_tab_for_untouched_files(tmp_path: Path) -> None:
    """Reconciliation only re-syncs tabs already open; it does not open new
    ones -- new-file discovery is the workspace tree's job."""
    _app()
    (tmp_path / "open.py").write_text("kept\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        pane.open_file(tmp_path / "open.py")

        (tmp_path / "new_from_shell.py").write_text("new\n", encoding="utf-8")
        projection.reconcile_workspace()

        assert pane.path_editor(pane.resolve_workspace_path("new_from_shell.py")) is None
        assert pane.path_editor(pane.resolve_workspace_path("open.py")).toPlainText() == "kept\n"
    finally:
        pane.deleteLater()


def test_reconcile_workspace_leaves_unchanged_tabs_untouched(tmp_path: Path) -> None:
    _app()
    target = tmp_path / "stable.py"
    target.write_text("same\n", encoding="utf-8")
    pane, projection = _make_pane(tmp_path)
    try:
        pane.open_file(target)
        editor = pane.path_editor(pane.resolve_workspace_path("stable.py"))

        projection.reconcile_workspace()

        assert editor.toPlainText() == "same\n"
        assert editor.extraSelections() == []
    finally:
        pane.deleteLater()
