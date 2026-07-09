"""Focused tests for EventRelayTerminalTracker.handle_tool_result.

Covers validation truth field copying, validation_ok computation, and
bus event emission for classified vs unclassified validation commands.
"""

from __future__ import annotations

from aura.bridge.event_relay_terminal_tracking import EventRelayTerminalTracker
from aura.events import WORKER_COMMAND_FINISHED, WORKER_VALIDATION_FINISHED


def _make_tracker() -> tuple[EventRelayTerminalTracker, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    tracker = EventRelayTerminalTracker(emit_bus_event=lambda topic, payload: events.append((topic, payload)))
    return tracker, events


class TestEventRelayTerminalTracker:
    """Validates validation truth field propagation and classification."""

    def test_passing_classified_validation(self) -> None:
        """A passing classified validation produces validation_ok=True."""
        tracker, events = _make_tracker()
        parsed = {
            "tool_name": "run_terminal_command",
            "command": "pytest tests/test_example.py -q",
            "ok": True,
            "exit_code": 0,
            "validation_classification": "passed",
            "classification": "passed",
            "counts_as_validation": True,
            "counts_as_product_failure": False,
            "output": "=== 10 passed ===",
        }
        tracker.handle_tool_result("run_terminal_command", parsed)

        assert len(tracker.terminal_results) == 1
        assert len(tracker.validation_results) == 1
        record = tracker.validation_results[0]
        assert record["validation_ok"] is True
        assert record["counts_as_validation"] is True
        assert record["validation_classification"] == "passed"

        # Check bus events — both command finished and validation finished
        assert len(events) == 2
        cmd_topic, cmd_payload = events[0]
        assert cmd_topic == WORKER_COMMAND_FINISHED
        assert cmd_payload["ok"] is True
        val_topic, val_payload = events[1]
        assert val_topic == WORKER_VALIDATION_FINISHED
        assert val_payload["ok"] is True

    def test_failing_classified_validation(self) -> None:
        """A failing classified validation produces validation_ok=False."""
        tracker, events = _make_tracker()
        parsed = {
            "tool_name": "run_terminal_command",
            "command": "pytest tests/test_example.py -q",
            "ok": False,
            "exit_code": 1,
            "validation_classification": "product_validation_failed",
            "classification": "product_validation_failed",
            "counts_as_validation": True,
            "counts_as_product_failure": True,
            "output": "FAILED test_example.py::test_foo",
        }
        tracker.handle_tool_result("run_terminal_command", parsed)

        assert len(tracker.terminal_results) == 1
        assert len(tracker.validation_results) == 1
        record = tracker.validation_results[0]
        assert record["validation_ok"] is False

        # Bus event — validation finished with ok=False
        val_topic, val_payload = events[1]
        assert val_topic == WORKER_VALIDATION_FINISHED
        assert val_payload["ok"] is False

    def test_non_validation_command_skipped(self) -> None:
        """A command with exit_code=0 but no validation classification yields
        validation_ok=False (never True just because exit_code is 0)."""
        tracker, events = _make_tracker()
        parsed = {
            "tool_name": "run_terminal_command",
            "command": "echo hello",
            "ok": True,
            "exit_code": 0,
            "output": "hello",
        }
        tracker.handle_tool_result("run_terminal_command", parsed)

        assert len(tracker.terminal_results) == 1
        # _is_validation_terminal_record may still heuristic-detect it or not;
        # the guard is that validation_ok is never True for exit_code=0 alone.
        for rec in tracker.validation_results:
            assert rec["validation_ok"] is False

        # Bus event — validation_finished should only fire if the heuristic
        # detected it; if it didn't, there's only 1 event.  Either way the
        # validation_ok must not be True.
        cmd_events = [(t, p) for t, p in events if t == WORKER_COMMAND_FINISHED]
        assert len(cmd_events) == 1
        for t, p in events:
            if t == WORKER_VALIDATION_FINISHED:
                assert p["ok"] is False

    def test_passing_with_command_outcome_classification(self) -> None:
        """command_outcome_classification='passed' with counts_as_validation
        yields validation_ok=True."""
        tracker, events = _make_tracker()
        parsed = {
            "tool_name": "run_terminal_command",
            "command": "ruff check .",
            "ok": True,
            "exit_code": 0,
            "command_outcome_classification": "passed",
            "counts_as_validation": True,
            "output": "All checks passed!",
        }
        tracker.handle_tool_result("run_terminal_command", parsed)

        assert len(tracker.terminal_results) == 1
        assert len(tracker.validation_results) == 1
        record = tracker.validation_results[0]
        assert record["validation_ok"] is True

    def test_auto_validation_still_detected(self) -> None:
        """auto_validation=True without a 'passed' classification yields
        validation_ok=False."""
        tracker, events = _make_tracker()
        parsed = {
            "tool_name": "run_terminal_command",
            "command": "pytest tests/",
            "ok": True,
            "exit_code": 0,
            "auto_validation": True,
            "output": "=== 10 passed ===",
        }
        tracker.handle_tool_result("run_terminal_command", parsed)

        assert len(tracker.terminal_results) == 1
        assert len(tracker.validation_results) == 1
        record = tracker.validation_results[0]
        assert record["validation_ok"] is False
