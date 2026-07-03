"""Tests for the --version CLI flag in aura/__main__.py."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

from aura.__main__ import _parse_args, _run_app
from aura.version import __version__


class TestParseArgsVersion:
    """_parse_args correctly recognizes and defaults --version."""

    def test_parse_args_recognizes_version_flag(self) -> None:
        args, qt_argv = _parse_args(["--version"])
        assert args.version is True
        assert qt_argv == [sys.argv[0]]

    def test_parse_args_without_version(self) -> None:
        args, qt_argv = _parse_args([])
        assert args.version is False
        # qt_argv always includes sys.argv[0]
        assert qt_argv == [sys.argv[0]]


class TestRunAppVersion:
    """_run_app with --version behaves correctly."""

    def test_version_prints_and_returns_zero(self) -> None:
        args, _ = _parse_args(["--version"])
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            result = _run_app(Path("/tmp/dummy.log"), args, [sys.argv[0]])
        assert result == 0
        output = captured.getvalue().strip()
        assert output == f"Aura {__version__}"

    def test_version_does_not_import_pyside(self) -> None:
        """--version returns before PySide6 is imported."""
        # Ensure PySide6 is not in sys.modules before the call
        was_present = "PySide6" in sys.modules
        args, _ = _parse_args(["--version"])
        with mock.patch("sys.stdout", io.StringIO()):
            result = _run_app(Path("/tmp/dummy.log"), args, [sys.argv[0]])
        assert result == 0
        # If it wasn't present before, it shouldn't be now
        if not was_present:
            assert "PySide6" not in sys.modules, (
                "--version must not trigger PySide6 import"
            )
