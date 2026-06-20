"""Tests for aura/sandbox.py — WatchResult, classify_watch_outcome, and run_and_watch.

All verdict tests call classify_watch_outcome directly with canned inputs.
No subprocess involvement.
"""

from __future__ import annotations

import pytest

from aura.sandbox import WatchResult, classify_watch_outcome


class TestWatchResult:
    """Coverage area 1: dataclass instantiation, frozen=True, all fields."""

    def test_instantiation(self):
        result = WatchResult(
            ok=True,
            survived_window=True,
            exited_early=False,
            error_detected=False,
            exit_code=None,
            output="started ok\n",
        )
        assert result.ok is True
        assert result.survived_window is True
        assert result.exited_early is False
        assert result.error_detected is False
        assert result.exit_code is None
        assert result.output == "started ok\n"

    def test_frozen(self):
        result = WatchResult(
            ok=False,
            survived_window=False,
            exited_early=True,
            error_detected=True,
            exit_code=1,
            output="err",
        )
        with pytest.raises((AttributeError, TypeError)):
            result.ok = True  # type: ignore[misc]


class TestClassifyWatchOutcome:
    """Coverage area 2: verdict contract — one test per case."""

    def test_survived_clean(self):
        """still_running=True, no traceback → ok=True, survived_window=True."""
        result = classify_watch_outcome(
            still_running=True,
            exit_code=None,
            output="started ok\n",
            window_seconds=10,
        )
        assert result.ok is True
        assert result.survived_window is True
        assert result.exited_early is False
        assert result.error_detected is False
        assert result.exit_code is None

    def test_survived_with_traceback(self):
        """still_running=True, output contains traceback → ok=False, error_detected=True."""
        result = classify_watch_outcome(
            still_running=True,
            exit_code=None,
            output="Traceback (most recent call last):\n  File \"test.py\", line 1, in <module>\nValueError: bad",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.survived_window is True
        assert result.error_detected is True
        assert result.exited_early is False
        assert result.exit_code is None

    def test_early_exit_zero(self):
        """still_running=False, exit_code=0, no traceback → ok=True, exited_early=True."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=0,
            output="done\n",
            window_seconds=10,
        )
        assert result.ok is True
        assert result.exited_early is True
        assert result.survived_window is False
        assert result.error_detected is False
        assert result.exit_code == 0

    def test_early_exit_nonzero(self):
        """still_running=False, exit_code=1, no traceback → ok=False, exited_early=True."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=1,
            output="err\n",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.exited_early is True
        assert result.survived_window is False
        assert result.error_detected is False
        assert result.exit_code == 1

    def test_traceback_overrides_early_exit_zero(self):
        """still_running=False, exit_code=0, traceback → ok=False, error_detected=True."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=0,
            output="Traceback (most recent call last):\n  File \"x.py\", line 5, in <module>\nZeroDivisionError: div by zero",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.error_detected is True
        assert result.exited_early is True
        assert result.survived_window is False
        assert result.exit_code == 0

    def test_traceback_overrides_early_exit_nonzero(self):
        """still_running=False, exit_code=2, traceback → ok=False, error_detected=True."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=2,
            output="Traceback (most recent call last):\n  File \"y.py\", line 1, in <module>\nKeyError: 'x'",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.error_detected is True
        assert result.exited_early is True
        assert result.survived_window is False
        assert result.exit_code == 2

    def test_traceback_overrides_survived(self):
        """still_running=True, exit_code=None, traceback → ok=False, error_detected=True."""
        result = classify_watch_outcome(
            still_running=True,
            exit_code=None,
            output="Traceback (most recent call last):\n  File \"z.py\", line 3, in <module>\nRuntimeError: crash",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.error_detected is True
        assert result.survived_window is True
        assert result.exited_early is False
        assert result.exit_code is None
