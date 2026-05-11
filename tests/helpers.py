"""Shared test helpers for git-related tests.

Provides ``MockResult`` (a dataclass simulating ``subprocess.CompletedProcess``)
and ``_make_run`` (a factory that builds a mock ``subprocess.run`` from a list
of side effects).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class MockResult:
    """Simulates ``subprocess.CompletedProcess`` for testing.

    Attributes match those that the production code accesses (returncode,
    stdout, stderr).
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _make_run(side_effects: list[Any]) -> Callable[..., Any]:
    """Build a ``subprocess.run`` mock that yields from *side_effects*.

    Each element of *side_effects* is either:
      - A ``MockResult`` instance — returned directly (or turned into a
        ``CalledProcessError`` if ``check=True`` and ``returncode != 0``).
      - An ``Exception`` instance — raised directly.

    When ``check=True`` and the result's ``returncode != 0`` the mock raises
    ``subprocess.CalledProcessError`` with ``stderr``/``stdout`` left as
    strings when ``text=True`` was passed (matching real subprocess behaviour),
    or encoded to bytes when ``text`` was not passed.
    """
    calls = list(reversed(side_effects))

    def _run(*args: Any, **kwargs: Any) -> Any:
        item = calls.pop()
        if isinstance(item, BaseException):
            raise item
        if kwargs.get("check") and item.returncode != 0:
            cmd = args[0] if args else kwargs.get("cmd", [])
            is_text = kwargs.get("text", False)
            stderr_val = item.stderr
            stdout_val = item.stdout
            if not is_text:
                if isinstance(stderr_val, str):
                    stderr_val = stderr_val.encode()
                if isinstance(stdout_val, str):
                    stdout_val = stdout_val.encode()
            raise subprocess.CalledProcessError(
                item.returncode, cmd, output=stdout_val, stderr=stderr_val,
            )
        return item

    return _run
