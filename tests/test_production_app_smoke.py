"""Application-level smoke path for direct production execution.

Drives the *real* ConversationBridge, the real ToolRegistry against a real
temporary workspace, and the real GUI projection (ChatView, AuraPlayground,
WorkerEventHandler) offscreen, using a scripted backend sequence that performs:

    reasoning → TODO update → repository read → approved write producing a diff
    → terminal validation failure → corrective edit → terminal validation success
    → final response → receipt → idle transition

This is deliberately not a mocked ToolCatalog assertion: files are really
written and the validation commands really run.
"""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from aura.client import (  # noqa: E402
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.model_streams import (  # noqa: E402
    PLANNER_STREAM_HOOK,
    PRODUCTION_STREAM_HOOK,
    WORKER_STREAM_HOOK,
    model_streams,
)
from aura.worker_todo import UPDATE_WORKER_TODO_TOOL  # noqa: E402

VALIDATION_COMMAND = "python -m pytest -q test_calc.py"

# The corrective edit deliberately changes the file *length* as well as the
# operator: CPython invalidates a cached .pyc on (mtime, size), and two writes
# of equal size inside the same mtime tick would let a stale .pyc win.
BROKEN_CALC = "def add(a, b):\n    return a - b\n"
FIXED_CALC = (
    "def add(a, b):\n"
    '    """Return the sum of a and b."""\n'
    "    return a + b\n"
)


def _read_settled(
    path: Path, expected: str, attempts: int = 40, delay: float = 0.05
) -> str:
    """Read *path*, tolerating brief filesystem-visibility lag on Windows.

    The tool writes on the bridge's worker thread; the freshly replaced file is
    occasionally not yet visible to a read issued immediately afterwards in
    this process. Polls up to ~2s for *expected*, then returns whatever it last
    saw so a genuine mismatch still fails the assertion.
    """
    import time

    content = path.read_text(encoding="utf-8")
    for _ in range(attempts):
        if content == expected:
            return content
        time.sleep(delay)
        content = path.read_text(encoding="utf-8")
    return content


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(
        "Toy project. Validation: python -m pytest -q test_calc.py\n",
        encoding="utf-8",
    )
    (root / "test_calc.py").write_text(
        textwrap.dedent(
            """\
            from calc import add


            def test_add():
                assert add(2, 2) == 4
            """
        ),
        encoding="utf-8",
    )
    return root


# ── scripted backend ────────────────────────────────────────────────────────


def _tool_round(calls: list[tuple[str, str, dict]], text: str = "") -> list:
    """Build one assistant round that requests tool calls."""
    events: list = []
    if text:
        events.append(ContentDelta(text=text))
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={"role": "assistant", "content": text, "tool_calls": tool_calls},
    ))
    return events


def _final_round(text: str) -> list:
    return [
        ContentDelta(text=text),
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": text},
        ),
    ]


class ScriptedProductionBackend:
    """Yields a fixed sequence of model rounds and records every invocation."""

    def __init__(self, rounds: list[list]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(_final_round("(script exhausted)"))


class NeverCalledBackend:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError(f"{self.label} backend must not run during normal coding")


# ── GUI harness ─────────────────────────────────────────────────────────────


class ProductionAppHarness:
    """Real bridge + real GUI projection, wired exactly as MainWindow does."""

    def __init__(self, workspace: Path) -> None:
        from aura.bridge import ConversationBridge
        from aura.config import AppSettings
        from aura.gui.chat_view import ChatView
        from aura.gui.playground import AuraPlayground
        from aura.gui.worker_handler import WorkerEventHandler

        self.parent = QWidget()
        self.settings = AppSettings.from_dict({})
        self.bridge = ConversationBridge(
            parent_widget=self.parent, provider=self.settings.provider
        )
        self.bridge.set_production_provider(self.settings.provider)
        self.bridge.set_workspace_root(workspace)
        self.bridge.set_production_mode()
        self.bridge.set_auto_approve(True)

        self.chat = ChatView()
        self.chat.setParent(self.parent)
        self.chat.set_compact_tools(True)
        self.playground = AuraPlayground(parent=self.parent)
        self.playground.set_workspace_root(workspace)

        self.worker_handler = WorkerEventHandler(
            bridge=self.bridge,
            chat=self.chat,
            playground=self.playground,
            settings=self.settings,
            parent=self.parent,
        )
        self.worker_handler.connect_bridge_signals()

        # Observations
        self.started: list[str] = []
        self.finished: list[tuple] = []
        self.todo_snapshots: list[tuple[str, list]] = []
        self.activity_snapshots: list[tuple[str, list]] = []
        self.diffs: list[tuple] = []
        self.tool_results: list[tuple] = []
        self.terminal_output: list[tuple] = []
        self.chat_content: list[str] = []
        self.running_states: list[bool] = []

        self.bridge.workerStarted.connect(lambda r: self.started.append(r))
        self.bridge.workerFinished.connect(lambda *a: self.finished.append(a))
        self.bridge.workerTodoUpdated.connect(lambda *a: self.todo_snapshots.append(a))
        self.bridge.workerActivityUpdated.connect(
            lambda *a: self.activity_snapshots.append(a)
        )
        self.bridge.workerDiffDecided.connect(lambda *a: self.diffs.append(a))
        self.bridge.workerToolResult.connect(lambda *a: self.tool_results.append(a))
        self.bridge.workerTerminalOutput.connect(
            lambda *a: self.terminal_output.append(a)
        )
        self.bridge.contentDelta.connect(lambda t: self.chat_content.append(t))
        self.worker_handler.worker_running_changed.connect(
            lambda v: self.running_states.append(v)
        )

    def worker_log_text(self) -> str:
        """The workspace Worker Log's visible text."""
        return self.playground._info_hub._log_view.toPlainText()

    def run_turn(self, user_text: str, timeout_ms: int = 120000) -> bool:
        """Send one production turn and pump the Qt loop until it completes."""
        self.bridge.history.set_system("You are Aura's production coding agent.")
        self.bridge.history.append_user_text(user_text)
        self.chat.add_user(user_text)
        self.chat.begin_assistant()

        loop = QEventLoop()
        completed = {"value": False}

        def _done() -> None:
            completed["value"] = True
            loop.quit()

        self.bridge.finished.connect(_done)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)

        self.bridge.send(model="scripted-production-model", thinking="off")
        loop.exec()
        timer.stop()
        # Let queued cross-thread signals drain.
        for _ in range(50):
            QApplication.processEvents()
        return completed["value"]

    def teardown(self) -> None:
        try:
            self.playground.terminal_window().close()
        except Exception:
            pass
        self.parent.deleteLater()
        QApplication.processEvents()


def install_backends(production, planner=None, worker=None) -> None:
    """Install scripted stream handlers.

    Must be called *after* the bridge is constructed: ConversationBridge owns
    the production hook and registers its own backend on construction and on
    every provider change.
    """
    for name, handler in (
        (PRODUCTION_STREAM_HOOK, production),
        (PLANNER_STREAM_HOOK, planner),
        (WORKER_STREAM_HOOK, worker),
    ):
        if handler is None:
            continue
        model_streams.unregister(name)
        model_streams.register(name, handler)


@pytest.fixture
def restore_streams():
    """Snapshot and restore the module-level stream registry."""
    saved = {
        name: model_streams.get_handler(name)
        for name in (PRODUCTION_STREAM_HOOK, PLANNER_STREAM_HOOK, WORKER_STREAM_HOOK)
    }
    yield
    for name, handler in saved.items():
        model_streams.unregister(name)
        if handler is not None:
            model_streams.register(name, handler)


@pytest.fixture
def full_script() -> list[list]:
    """The scripted production sequence required by the smoke path."""
    return [
        # 1. reasoning + live TODO
        [
            ReasoningDelta(text="Reading the project before touching anything.\n"),
            *_tool_round([(
                "todo-1",
                UPDATE_WORKER_TODO_TOOL,
                {"items": [
                    {"id": "inspect", "text": "Inspect the project", "status": "active"},
                    {"id": "implement", "text": "Implement add()", "status": "pending"},
                    {"id": "validate", "text": "Run the test suite", "status": "pending"},
                ]},
            )]),
        ],
        # 2. repository read
        _tool_round([("read-1", "read_file", {"path": "test_calc.py"})]),
        # 3. the first edit — the ordinary loop simply continues after it
        [
            ReasoningDelta(text="test_calc imports calc.add; creating it.\n"),
            *_tool_round([
                ("write-1", "write_file", {"path": "calc.py", "content": BROKEN_CALC}),
            ]),
        ],
        # 4. TODO advances now that the edit landed, then terminal validation —
        #    which fails
        [
            *_tool_round([(
                "todo-2",
                UPDATE_WORKER_TODO_TOOL,
                {"items": [
                    {"id": "inspect", "text": "Inspect the project", "status": "done"},
                    {"id": "implement", "text": "Implement add()", "status": "active"},
                    {"id": "validate", "text": "Run the test suite", "status": "pending"},
                ]},
            )]),
        ],
        _tool_round([("cmd-1", "run_terminal_command", {"command": VALIDATION_COMMAND})]),
        # 5. diagnosis + corrective edit
        [
            ReasoningDelta(text="add() subtracts. Fixing the operator.\n"),
            *_tool_round([
                ("write-2", "write_file", {"path": "calc.py", "content": FIXED_CALC}),
            ]),
        ],
        # 6. terminal validation rerun — passes
        _tool_round([("cmd-2", "run_terminal_command", {"command": VALIDATION_COMMAND})]),
        # 7. TODO complete + final response
        [
            *_tool_round([(
                "todo-3",
                UPDATE_WORKER_TODO_TOOL,
                {"items": [
                    {"id": "inspect", "text": "Inspect the project", "status": "done"},
                    {"id": "implement", "text": "Implement add()", "status": "done"},
                    {"id": "validate", "text": "Run the test suite", "status": "done"},
                ]},
            )]),
        ],
        _final_round(
            "Added calc.add. First pytest run failed (a - b); fixed to a + b and "
            "reran pytest, which passed."
        ),
    ]


@pytest.mark.skipif(
    shutil.which(sys.executable) is None, reason="python interpreter unavailable"
)
class TestProductionAppSmoke:
    def test_full_production_turn(
        self, qapp, workspace, full_script, restore_streams
    ) -> None:
        backend = ScriptedProductionBackend(full_script)
        planner = NeverCalledBackend("planner")
        worker = NeverCalledBackend("worker")

        harness = ProductionAppHarness(workspace)
        install_backends(backend.stream, planner.stream, worker.stream)
        try:
            completed = harness.run_turn(
                "Implement add() in calc.py so the test suite passes."
            )
            assert completed, "production turn did not finish"

            # --- one production backend owned the turn ----------------------
            assert backend.calls, "production backend never ran"
            assert planner.calls == []
            assert worker.calls == []
            assert all(
                call["model"] == "scripted-production-model" for call in backend.calls
            )
            # The turn consumed exactly the scripted rounds — no extra model
            # round was injected, so the assertions below line up with the
            # script rather than silently drifting.
            assert len(backend.calls) == len(full_script), (
                f"expected {len(full_script)} model rounds, got {len(backend.calls)}"
            )

            # --- the original conversation reached the model ----------------
            first_messages = backend.calls[0]["messages"]
            assert any(
                "Implement add() in calc.py" in str(m.get("content") or "")
                for m in first_messages
            )

            # --- the file work really happened ------------------------------
            # Aura's own execution ledger is the authority on what was applied.
            applied = [
                w.get("path") for w in
                harness.bridge.production_session.relay.write_results
            ]
            write_tool_results = [
                (t[1], t[3], t[4])
                for t in harness.tool_results
                if t[2] == "write_file"
            ]
            assert applied == ["calc.py", "calc.py"], (
                f"expected the initial write and the corrective edit, got {applied}; "
                f"write tool results: {write_tool_results}"
            )
            # Cross-check on disk (settle-tolerant: the write lands on the
            # worker thread and Windows can lag making it visible to a fresh
            # read in this process).
            calc = workspace / "calc.py"
            assert calc.exists()
            assert _read_settled(calc, FIXED_CALC) == FIXED_CALC

            # --- workspace lifecycle ---------------------------------------
            assert len(harness.started) == 1
            run_id = harness.started[0]
            assert run_id.startswith("prod-")

            # --- live TODO reached the GUI with one active item ------------
            assert harness.todo_snapshots, "TODO never projected"
            assert all(rid == run_id for rid, _ in harness.todo_snapshots)
            mid = harness.todo_snapshots[1][1]
            assert sum(1 for i in mid if i["status"] == "active") == 1
            final_todo = harness.todo_snapshots[-1][1]
            assert all(i["status"] == "done" for i in final_todo)

            # --- activity projection ---------------------------------------
            assert harness.activity_snapshots

            # --- approved write produced a diff ----------------------------
            assert harness.diffs, "approved write did not project a diff"
            diff_paths = {d[3] for d in harness.diffs}
            assert "calc.py" in diff_paths
            assert all(d[2] in ("approve", "approve_all") for d in harness.diffs)

            # --- terminal output projected ---------------------------------
            assert harness.terminal_output, "terminal output did not project"
            assert all(t[0] == run_id for t in harness.terminal_output)

            # --- failure, repair, and rerun are one turn --------------------
            evidence = harness.bridge.production_session.evidence()
            validations = evidence.validation_results
            assert len(validations) >= 2, f"expected 2 validation runs, got {validations}"
            assert validations[0]["exit_code"] != 0
            assert validations[-1]["exit_code"] == 0

            # --- exactly one completion receipt ----------------------------
            assert len(harness.finished) == 1
            finished_run_id, ok, summary, needs_followup, status = harness.finished[0]
            assert finished_run_id == run_id
            assert ok is True
            assert status == "completed"
            assert needs_followup is False
            assert "calc.py" in summary
            assert "after repair" in summary
            assert "failed, repaired, passed on rerun" in summary
            # The receipt reports execution facts only. The final answer is
            # chat-owned and must not be restated here.
            assert "reran pytest" not in summary

            # --- receipt cites real run data -------------------------------
            metadata = harness.bridge.execution_result_metadata(run_id)
            assert metadata["modified_files"] == ["calc.py"]
            assert metadata["validation_repaired"] is True
            assert metadata["commands_run"].count(VALIDATION_COMMAND) >= 2
            # The prose is still available as metadata for diagnostics.
            assert "reran pytest" in metadata["final_response"]

            # --- workspace returned to idle --------------------------------
            assert harness.running_states[-1] is False
            assert harness.bridge.production_session.is_active() is False
            assert harness.bridge.is_running() is False

            # --- the assistant's prose reached the chat, exactly once -------
            # Chat owns conversation prose even on the production path.
            assert "".join(harness.chat_content).count("reran pytest") == 1, (
                f"assistant prose must reach chat exactly once, got "
                f"{harness.chat_content!r}"
            )
            # ...and was not also duplicated into the ephemeral Worker Log.
            worker_log = harness.worker_log_text()
            assert "reran pytest" not in worker_log, (
                "assistant prose must not be duplicated into the Worker Log"
            )

            # --- no SpecCard / dispatch during normal coding ---------------
            assert harness.bridge.dispatch_records == []
            assert harness.chat.get_spec_card(run_id) is None
            offered = {
                item["function"]["name"]
                for call in backend.calls
                for item in call["tools"]
                if isinstance(item.get("function"), dict)
            }
            assert "dispatch_to_worker" not in offered
            assert UPDATE_WORKER_TODO_TOOL in offered

        finally:
            harness.teardown()

    def test_undo_snapshot_is_captured_before_a_production_turn(
        self, qapp, workspace, restore_streams
    ) -> None:
        """Proof 16: pre-run snapshot capture still happens for /undo."""
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git unavailable")
        env_args = dict(cwd=workspace, capture_output=True, text=True)
        subprocess.run(["git", "init", "-q"], **env_args)
        subprocess.run(["git", "config", "user.email", "t@example.com"], **env_args)
        subprocess.run(["git", "config", "user.name", "Test"], **env_args)
        subprocess.run(["git", "add", "-A"], **env_args)
        subprocess.run(["git", "commit", "-q", "-m", "init"], **env_args)

        backend = ScriptedProductionBackend([_final_round("Nothing to change.")])
        harness = ProductionAppHarness(workspace)
        install_backends(backend.stream)
        try:
            assert harness.bridge.get_pre_worker_snapshot() is None
            assert harness.run_turn("Have a look around.", timeout_ms=60000)
            snapshot_sha = harness.bridge.get_pre_worker_snapshot()
            assert snapshot_sha, "production turn must capture a pre-run snapshot"
            assert len(snapshot_sha) >= 7
        finally:
            harness.teardown()

    def test_plain_conversational_turn_still_works(
        self, qapp, workspace, restore_streams
    ) -> None:
        """Direct conversational operation: no tools, one receipt, back to idle."""
        backend = ScriptedProductionBackend([
            _final_round("Aura keeps its production settings in aura/settings.py."),
        ])
        harness = ProductionAppHarness(workspace)
        install_backends(backend.stream)
        try:
            assert harness.run_turn("Where do settings live?", timeout_ms=30000)
            assert len(backend.calls) == 1
            assert len(harness.finished) == 1
            _run_id, ok, summary, _needs, status = harness.finished[0]
            assert ok is True
            assert status == "completed"
            assert "Files changed   : 0" in summary
            # The answer belongs to the chat, not to the execution receipt.
            assert "aura/settings.py" not in summary
            assert "".join(harness.chat_content).count("aura/settings.py") == 1
            assert harness.bridge.production_session.is_active() is False
        finally:
            harness.teardown()

    def test_cancellation_stops_execution_cleanly(
        self, qapp, workspace, restore_streams
    ) -> None:
        """Proof 15: cancelling mid-turn returns the workspace to idle."""
        import threading

        entered = threading.Event()

        class _BlockingBackend:
            calls: list = []

            def stream(self, **kwargs):
                self.calls.append(kwargs)
                cancel_event = kwargs.get("cancel_event")
                entered.set()
                # Wait for the user's Stop, then end the stream like a real
                # provider client does.
                if cancel_event is not None:
                    cancel_event.wait(timeout=20)
                return iter(())

        backend = _BlockingBackend()
        harness = ProductionAppHarness(workspace)
        install_backends(backend.stream)
        try:
            harness.bridge.history.set_system("system")
            harness.bridge.history.append_user_text("do something long")
            harness.chat.begin_assistant()

            loop = QEventLoop()
            harness.bridge.finished.connect(loop.quit)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(loop.quit)
            guard.start(30000)

            cancelled: list[str] = []
            harness.bridge.workerCancelled.connect(lambda r: cancelled.append(r))

            harness.bridge.send(model="m", thinking="off")
            assert entered.wait(timeout=10), "backend never started"
            harness.bridge.request_cancel()
            loop.exec()
            guard.stop()
            for _ in range(50):
                QApplication.processEvents()

            assert cancelled, "cancellation must reach the workspace"
            assert harness.finished == [], "a cancelled run must not emit a receipt"
            assert harness.bridge.production_session.is_active() is False
            assert harness.bridge.is_running() is False
            assert harness.running_states[-1] is False
        finally:
            harness.teardown()


# ── real MainWindow startup ─────────────────────────────────────────────────


class TestRealMainWindowStartup:
    """Construct the real MainWindow the way `python -m aura` does."""

    def _boot(self, tmp_path: Path, config: dict, monkeypatch):
        import json

        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "config.json").write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
        monkeypatch.setenv("AURA_DATA_DIR", str(profile))

        import aura.paths

        monkeypatch.setattr(aura.paths, "config_dir", lambda: profile)
        monkeypatch.setattr(aura.paths, "data_dir", lambda: profile)

        from aura.gui.main_window import MainWindow

        return MainWindow()

    def test_startup_with_legacy_config_enters_production_mode(
        self, qapp, tmp_path, monkeypatch
    ) -> None:
        """Startup loads old settings, migrates them, and runs production mode."""
        window = self._boot(
            tmp_path,
            {
                "planner_provider": "deepseek",
                "worker_provider": "deepseek",
                "planner_worker_mode": True,
                "default_planner_model": "deepseek-v4-flash",
                "default_worker_model": "deepseek-v4-pro",
                "default_planner_thinking": "max",
                "auto_dispatch": True,
                "restore_last_conversation": False,
                "first_launch_done": True,
            },
            monkeypatch,
        )
        try:
            # Settings migrated to the production configuration.
            assert window._settings.provider == "deepseek"
            assert window._settings.default_model == "deepseek-v4-flash"
            assert window._settings.default_thinking == "max"
            # Legacy fields survive the load.
            assert window._settings.planner_worker_mode is True
            assert window._settings.default_worker_model == "deepseek-v4-pro"

            # The live bridge is nevertheless in production single-agent mode.
            assert window._bridge.planner_worker_mode is False
            assert window._bridge.registry.mode == "single"

            # The production tool surface is what the model will see.
            names = {
                item["function"]["name"]
                for item in window._bridge.registry.tool_defs()
                if isinstance(item.get("function"), dict)
            }
            assert UPDATE_WORKER_TODO_TOOL in names
            assert "dispatch_to_worker" not in names

            # The visible production model selection is the migrated one.
            assert window.current_model() == "deepseek-v4-flash"
            assert window.current_thinking() == "max"

            # Signal wiring is live: worker lifecycle reaches the workspace.
            assert window._worker_handler is not None
            run_id = window._bridge.production_session.begin(model="m")
            QApplication.processEvents()
            assert window._worker_handler._is_production_run(run_id)

            # Auto-dispatch is not part of the normal product UI.
            assert window._toolbar._auto_dispatch_switch.isVisible() is False
        finally:
            window.close()
            window.deleteLater()
            QApplication.processEvents()

    def test_startup_with_empty_config(self, qapp, tmp_path, monkeypatch) -> None:
        window = self._boot(tmp_path, {}, monkeypatch)
        try:
            assert window._bridge.planner_worker_mode is False
            assert window._bridge.registry.mode == "single"
            assert window._settings.planner_worker_mode is False
        finally:
            window.close()
            window.deleteLater()
            QApplication.processEvents()
