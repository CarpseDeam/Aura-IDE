"""A failed command is not progress, and does not reopen pre-edit planning.

The production failure this pins down: ``PreEditLoopGuard.record()`` marked
every terminal call as forward progress *before the command ran*, and
``observe_result()`` then handed the failure a round of reread grace without
undoing that progress. A command that failed therefore reset the stall counter
and unlocked rereads at the same time, so a turn could fail the same validation
over and over while the guard reported movement and never steered.

The contract asserted here:

* tool intent proves nothing — only results do;
* an applied write is progress;
* a **successful** terminal or diagnostic result is progress;
* a **failed** command is not, so the round stays stagnant and steering fires;
* one distinct failure buys one recovery round; repeating the same command into
  the same failure renews nothing and is not new evidence;
* a corrected command is never blocked, and when it succeeds stagnation resets;
* driven through the real manager loop, a turn that starts with a failing
  validation still reaches an edit — or a clear stop — in a bounded number of
  rounds, with its tool output and structured failures intact.
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
    MAX_STAGNANT_ROUNDS_BEFORE_STEER,
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

STEER = MAX_STAGNANT_ROUNDS_BEFORE_STEER

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


# ── intent is not progress ──────────────────────────────────────────────────


class TestIntentIsNotProgress:

    @pytest.mark.parametrize("tool", sorted(COMMAND_TOOLS))
    def test_recording_a_command_call_does_not_claim_progress(self, tool: str) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record(tool, {"command": "pytest"})
        guard.end_round()

        assert guard.stagnant_rounds == 1, (
            "asking for a command is not evidence that anything moved"
        )

    def test_record_never_sets_the_round_progress_flag(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        for tool in sorted(COMMAND_TOOLS | {"write_file"}):
            guard.record(tool, {})

        assert guard._round_made_progress is False

    def test_the_diagnostic_tool_is_a_command_tool(self) -> None:
        assert "run_diagnostic_command" in DIAGNOSTIC_TOOLS
        assert DIAGNOSTIC_TOOLS <= COMMAND_TOOLS
        assert "run_terminal_command" in COMMAND_TOOLS


# ── results decide ──────────────────────────────────────────────────────────


class TestResultsDecideProgress:

    @pytest.mark.parametrize("tool", sorted(COMMAND_TOOLS))
    def test_a_failed_command_does_not_reset_stagnation(self, tool: str) -> None:
        guard = PreEditLoopGuard()
        guard.stagnant_rounds = 3

        failing_round(guard, tool, FAILED_COMMAND)

        assert guard.stagnant_rounds == 4, "a failure moves the turn backwards, not forwards"

    @pytest.mark.parametrize("tool", sorted(COMMAND_TOOLS))
    def test_a_successful_command_resets_stagnation(self, tool: str) -> None:
        guard = PreEditLoopGuard()
        guard.stagnant_rounds = 3

        succeeding_round(guard, tool, OK_COMMAND)

        assert guard.stagnant_rounds == 0

    def test_an_applied_write_is_progress(self) -> None:
        guard = PreEditLoopGuard()
        guard.stagnant_rounds = 3
        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, json.dumps({"applied": True}))
        guard.end_round()

        assert guard.write_applied is True
        assert guard.stagnant_rounds == 0

    def test_a_write_that_did_not_apply_is_not_progress(self) -> None:
        guard = PreEditLoopGuard()
        guard.stagnant_rounds = 3
        guard.begin_round()
        guard.record("write_file", {"path": "notes.md"})
        guard.observe_result("write_file", True, json.dumps({"applied": False}))
        guard.end_round()

        assert guard.write_applied is False
        assert guard.stagnant_rounds == 4


# ── failure grace is spent once per distinct failure ────────────────────────


class TestFailureGrace:

    def test_one_distinct_failure_buys_one_recovery_round(self) -> None:
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

    def test_repeating_the_same_failure_renews_nothing(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        for _ in range(4):
            failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)

        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is not None, (
            "retrying one broken command must not buy grace forever"
        )
        assert guard.repeated_failures == 3

    def test_a_genuinely_different_failure_buys_its_own_round(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)
        for _ in range(3):
            guard.begin_round()
            guard.end_round()
        assert guard.check("read_file", {"path": "notes.md"}) is not None

        failing_round(
            guard,
            "run_diagnostic_command",
            {**FAILED_COMMAND,
             "failure_class": "diagnostic_command_path_escapes_workspace",
             "requested_command": "cat ../../etc/passwd"},
        )
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is None

    def test_repeated_failures_are_not_new_evidence(self) -> None:
        guard = PreEditLoopGuard()
        for _ in range(5):
            failing_round(guard, "run_terminal_command", FAILED_COMMAND)

        assert guard.stagnant_rounds == 5
        assert guard.seen_evidence == set()

    def test_the_same_failure_has_one_fingerprint(self) -> None:
        first = failure_fingerprint("run_diagnostic_command", json.dumps(FAILED_COMMAND))
        again = failure_fingerprint("run_diagnostic_command", dict(FAILED_COMMAND))
        other = failure_fingerprint(
            "run_diagnostic_command",
            {**FAILED_COMMAND, "requested_command": "npm install right-pad"},
        )

        assert first == again
        assert first != other


# ── steering still fires, corrections still work ────────────────────────────


class TestSteeringSurvivesFailures:

    def test_repeated_command_failures_still_earn_the_steering_message(self) -> None:
        guard = PreEditLoopGuard()
        for _ in range(STEER):
            failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)

        message = guard.take_steering_message()
        assert "no new evidence" in message
        assert "write_file" in message

    def test_the_focus_budget_is_not_reopened_by_failures(self) -> None:
        guard = PreEditLoopGuard()
        for index in range(guard_module.MAX_DISCOVERY_CALLS_BEFORE_FOCUS):
            guard.begin_round()
            args = {"path": f"src/m{index}.py"}
            guard.record("read_file", args)
            guard.observe_result("read_file", True, json.dumps({"path": args["path"], "body": index}))
            guard.end_round()
        failing_round(guard, "run_terminal_command", FAILED_COMMAND)

        assert guard.take_focus_message() != ""

    def test_a_corrected_command_is_never_blocked(self) -> None:
        guard = PreEditLoopGuard()
        for _ in range(6):
            failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)

        for tool in sorted(COMMAND_TOOLS):
            assert guard.check(tool, {"command": "python -m pytest -q"}) is None

    def test_a_successful_correction_resets_stagnation_normally(self) -> None:
        guard = PreEditLoopGuard()
        for _ in range(4):
            failing_round(guard, "run_diagnostic_command", FAILED_COMMAND)
        assert guard.stagnant_rounds == 4

        succeeding_round(guard, "run_diagnostic_command", OK_COMMAND)

        assert guard.stagnant_rounds == 0


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
GOOD_COMMAND = 'python -c "print(2+2)"'


def steering_messages(manager) -> list[str]:
    return [
        str(m.get("content"))
        for m in manager.history.messages
        if m.get("aura_internal") and "Loop guard:" in str(m.get("content"))
    ]


class TestBoundedProgressionThroughTheRealLoop:
    """One scripted turn, driven through the production manager and registry."""

    def _script(self, tail: list) -> list:
        return [
            tool_round([("d0", "run_diagnostic_command", {"command": BAD_COMMAND})]),
            tool_round([("d1", "run_diagnostic_command", {"command": BAD_COMMAND})]),
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
        assert len(backend.calls) == 5, "the turn must not circle"
        # The edit really landed.
        assert (project / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nfixed body\n"
        )
        # The two failures did not launder themselves as progress: steering
        # fired, which the old code suppressed entirely.
        assert len(steering_messages(manager)) == 1
        assert "make the change now" in steering_messages(manager)[0]

    def test_the_failures_stay_visible_and_structured(
        self, project, isolated_streams  # noqa: F811
    ) -> None:
        rounds = self._script([
            tool_round([("d2", "run_diagnostic_command", {"command": GOOD_COMMAND})]),
            final_round("Corrected the command."),
        ])
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(project, "Validate the project.")
        recorder = Recorder()

        run(manager, recorder)

        results = [r for r in recorder.tool_results if r.name == "run_diagnostic_command"]
        assert [r.ok for r in results] == [False, False, True]
        rejected = json.dumps([m for m in manager.history.messages if m.get("role") == "tool"])
        assert "diagnostic_command_mutating" in rejected
        assert "install" in rejected
        # The corrected command's real output survives to the transcript.
        assert "4" in rejected

    def test_a_turn_that_only_fails_stops_instead_of_circling(
        self, project, isolated_streams  # noqa: F811
    ) -> None:
        rounds = self._script([
            tool_round([("d2", "run_diagnostic_command", {"command": BAD_COMMAND})]),
            final_round("Blocked: this command cannot run as a diagnostic."),
        ])
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(project, "Validate the project.")

        run(manager, Recorder())

        assert len(backend.calls) == 4
        assert len(steering_messages(manager)) == 1, (
            "one nudge, not one per failure"
        )
        assert (project / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        ), "nothing was written, and nothing pretended otherwise"


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
