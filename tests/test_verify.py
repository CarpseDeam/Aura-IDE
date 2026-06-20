from __future__ import annotations

import subprocess
from pathlib import Path

from aura.verify import _import_one_module
from tests.helpers import MockResult


class TestImportOneModule:
    """Tests for _import_one_module failure classification."""

    def test_success(self, monkeypatch) -> None:
        """returncode == 0 → ok is True, is_infra is False."""
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: MockResult(returncode=0),
        )
        ok, _output, is_infra = _import_one_module(
            Path("python"), "fake_module", Path("/fake")
        )
        assert ok is True
        assert is_infra is False

    def test_regression_guard(self, monkeypatch) -> None:
        """Traceback + shell marker → is_infra is False (traceback wins)."""
        stderr = (
            'Traceback (most recent call last):\n'
            '  File "/some/module.py", line 5, in <module>\n'
            '    open("no such file or directory path")\n'
            'FileNotFoundError: [Errno 2] No such file or directory\n'
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: MockResult(returncode=1, stderr=stderr),
        )
        ok, _output, is_infra = _import_one_module(
            Path("python"), "fake_module", Path("/fake")
        )
        assert ok is False
        assert is_infra is False

    def test_module_not_found_error(self, monkeypatch) -> None:
        """returncode != 0, clean ModuleNotFoundError traceback, no shell markers."""
        stderr = (
            'Traceback (most recent call last):\n'
            '  File "<string>", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'nonexistent'\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: MockResult(returncode=1, stderr=stderr),
        )
        ok, _output, is_infra = _import_one_module(
            Path("python"), "nonexistent", Path("/fake")
        )
        assert ok is False
        assert is_infra is False

    def test_shell_failure(self, monkeypatch) -> None:
        """returncode != 0, shell error with no traceback → is_infra is True."""
        stderr = (
            "'python' is not recognized as an internal or external command,"
            " operable program or batch file."
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *args, **kwargs: MockResult(returncode=1, stderr=stderr),
        )
        ok, _output, is_infra = _import_one_module(
            Path("python"), "fake_module", Path("/fake")
        )
        assert ok is False
        assert is_infra is True

    def test_subprocess_launch_failure(self, monkeypatch) -> None:
        """Mock raises FileNotFoundError → is_infra is True."""
        def _run_raising(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(subprocess, "run", _run_raising)
        ok, output, is_infra = _import_one_module(
            Path("python"), "fake_module", Path("/fake")
        )
        assert ok is False
        assert is_infra is True
        assert output
        assert "no such file" in output.lower()
