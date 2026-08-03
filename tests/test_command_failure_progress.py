"""A failed command is not progress, and failures stay rereadable.

The pre-edit guard's one gate is the exact-repeat read before the first applied
write.  Failures of any tool are truth: they open one round of reread grace so
the model can look again at whatever the failure contradicts, and they never
reset any boundary on intent alone — a command that is about to fail is not
progress because its result has not been seen.

The contract asserted here:

* tool intent proves nothing — only results do;
* an applied write is the one fact that flips the guard's ``write_applied``;
* a write that did not apply is never progress (fail-closed ``applied: True``);
* a failed result opens exactly one round of reread grace;
* driven through the real manager loop, a turn that starts with a failing
  command still reaches an edit in a bounded number of rounds, with its tool
  output and structured failures intact and every request on the stable
  catalog.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from aura.conversation import pre_edit_loop_guard as guard_module
from aura.conversation.pre_edit_loop_guard import (
    COMMAND_TOOLS,
    DIAGNOSTIC_TOOLS,
    PreEditLoopGuard,
    failure_fingerprint,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK
from tests.test_diagnostic_command_argv import VENV_PARTS, make_venv
from tests.test_single_pre_tool_narration import (
    Recorder,
    ScriptedBackend,
    build_manager,
    final_round,
    isolated_streams,  # noqa: F401 — pytest fixture
    run,
    tool_round,
)

FAILED_COMMAND = {
    "ok": False,
    "failure_class": "diagnostic_command_mutating",
    "requested_command": "npm install left-pad",
    "offending_token": "install",
    "error": "Command rejected: 'install' mutates state, and diagnostics are read-only.",
}
OK_COMMAND = {"ok": True, "exit_code": 0, "stdout": "4\n", "command": "python -c print(2+2)"}


def failing_round(guard: PreEditLoopGuard, tool: str, payload: dict) -> None:
    guard.begin_round()
    guard.record(tool, {"command": payload.get("requested_command", "")})
    guard.observe_result(tool, False, json.dumps(payload))
    guard.end_round()


def succeeding_round(guard: PreEditLoopGuard, tool: str, payload: dict) -> None:
    guard.begin_round()
    guard.record(tool, {"command": payload.get("command", "")})
    guard.observe_result(tool, True, json.dumps(payload))
    guard.end_round()


# ── results decide ──────────────────────────────────────────────────────────


class TestResultsDecideProgress:

    def test_an_applied_write_is_progress(self) -> None:
        guard = PreEditLoopGuard()

        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, json.dumps({"applied": True}))
        guard.end_round()

        assert guard.write_applied is True
        # After the write, the duplicate-read gate is dormant.
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is None

    def test_a_write_that_did_not_apply_is_not_progress(self) -> None:
        guard = PreEditLoopGuard()
        failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)

        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, json.dumps({"applied": False}))
        guard.end_round()

        assert guard.write_applied is False

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            json.dumps(["applied", True]),
            json.dumps({"ok": True, "path": "notes.md"}),
            json.dumps({"applied": "yes"}),
            json.dumps({"applied": 1}),
            None,
            42,
            {"ok": True},
        ],
    )
    def test_an_ambiguous_write_payload_is_never_an_applied_write(
        self, payload
    ) -> None:
        """Fail-closed: only an explicit ``applied: True`` proves a write landed.

        A malformed payload, a non-dictionary payload, a payload with no
        ``applied`` field, and a truthy-but-not-``True`` value are all "not
        applied" — matching the direct-write refresh contract.
        """
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, payload)
        guard.end_round()

        assert guard.write_applied is False

    def test_an_explicit_applied_true_payload_is_an_applied_write(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, {"ok": True, "applied": True})
        guard.end_round()

        assert guard.write_applied is True


# ── failure grace is spent per round ────────────────────────────────────────


class TestFailureGrace:

    def test_one_failure_buys_one_reread_round(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)

        # The recovery round may reread.
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is None
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        # The round after that is guarded again.
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is not None

    def test_a_failed_read_opens_the_same_reread_grace(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.observe_result("read_file", False, json.dumps({"error": "no such file"}))
        guard.end_round()

        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is None, (
            "the failed read's round grants one reread"
        )

    def test_the_same_failure_has_one_fingerprint(self) -> None:
        first = failure_fingerprint("run_diagnostic_command", json.dumps(FAILED_COMMAND))
        again = failure_fingerprint("run_diagnostic_command", dict(FAILED_COMMAND))
        other = failure_fingerprint(
            "run_diagnostic_command",
            {**FAILED_COMMAND, "requested_command": "npm install right-pad"},
        )

        assert first == again
        assert first != other


# ── the real manager loop ───────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real Python project with a real interpreter, in a path with a space."""
    root = tmp_path / "loop project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='loop'\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    make_venv(root, real=True)
    return root


BAD_COMMAND = "npm install left-pad"
# Read-only and allowed: `python -c` is refused outright, since whether an
# inline program only inspects is not a judgement the tool can make.
GOOD_COMMAND = "python --version"


class TestBoundedProgressionThroughTheRealLoop:
    """One scripted turn, driven through the production manager and registry."""

    def _script(self, tail: list) -> list:
        return [
            tool_round([("d0", "run_diagnostic_command", {"command": BAD_COMMAND})]),
            *tail,
        ]

    def test_a_failing_validation_still_reaches_an_edit(
        self, project, isolated_streams  # noqa: F811
    ) -> None:
        rounds = self._script([
            tool_round([("d2", "run_diagnostic_command", {"command": GOOD_COMMAND})]),
            tool_round([("w0", "write_file", {
                "path": "notes.md",
                "content": "# Notes\n\nfixed body\n",
            })]),
            final_round("Validation was rejected, corrected, and the edit applied."),
        ])
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(project, "Fix notes.md and validate it.")
        recorder = Recorder()

        run(manager, recorder)

        # Bounded: the scripted turn ran to its end, no extra rounds.
        assert len(backend.calls) == 4, "the turn must not circle"
        # The edit really landed.
        assert (project / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nfixed body\n"
        )
        # Every request used the one stable catalog — no narrowed request.
        assert backend.all_requests_stable() == [], backend.all_requests_stable()

    def test_the_failures_stay_visible_and_structured(
        self, project, isolated_streams  # noqa: F811
    ) -> None:
        rounds = self._script([
            tool_round([("d2", "run_diagnostic_command", {"command": GOOD_COMMAND})]),
            tool_round([("w0", "write_file", {
                "path": "notes.md",
                "content": "# Notes\n\ncorrected body\n",
            })]),
            final_round("Corrected the command and applied the edit."),
        ])
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(project, "Validate the project.")
        recorder = Recorder()

        run(manager, recorder)

        results = [r for r in recorder.tool_results if r.name == "run_diagnostic_command"]
        assert [r.ok for r in results] == [False, True]
        rejected = json.dumps([m for m in manager.history.messages if m.get("role") == "tool"])
        assert "diagnostic_command_mutating" in rejected
        assert "install" in rejected
        # The corrected command's real output survives to the transcript.
        assert "Python" in rejected


def test_no_new_owner_was_introduced() -> None:
    """No effort router, phase machine, or Planner/Worker behaviour was added."""
    import inspect

    source = inspect.getsource(guard_module)
    for banned in ("Manager", "Workflow", "Planner", "Phase", "effort", "complexity"):
        assert f"class {banned}" not in source
    classes = [
        name
        for name, value in vars(guard_module).items()
        if inspect.isclass(value) and value.__module__ == guard_module.__name__
    ]
    assert classes == ["PreEditLoopGuard"]


def test_the_real_interpreter_fixture_is_a_real_interpreter(project) -> None:
    """Guards the loop tests above: a stub would make them prove nothing."""
    interpreter = project.joinpath(".venv", *VENV_PARTS)
    assert interpreter.is_file()
    assert interpreter.stat().st_size == Path(sys.executable).stat().st_size
    assert shutil.which  # imported for make_venv's copy path
