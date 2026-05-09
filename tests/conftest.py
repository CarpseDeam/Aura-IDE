"""Shared pytest fixtures for the Aura test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Temporary workspace
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A temporary directory that serves as a workspace root.

    Creates a minimal file tree inside it:

        tmp_workspace/
          README.md
          pyproject.toml
          aura/
            __init__.py
            config.py
          scripts/
            smoke.py
          docs/
            notes.md

    The fixture also ensures that the current working directory is restored
    after the test completes.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "README.md").write_text("# Test Project")
    (ws / "pyproject.toml").write_text("[project]\nname='test'\n")
    aura_dir = ws / "aura"
    aura_dir.mkdir()
    (aura_dir / "__init__.py").write_text('"""Test aura package."""')
    (aura_dir / "config.py").write_text("VALUE = 42\n")
    scripts_dir = ws / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "smoke.py").write_text("print('hello')\n")
    docs_dir = ws / "docs"
    docs_dir.mkdir()
    (docs_dir / "notes.md").write_text("# Notes\nSome content.\n")
    # Hidden file and .git directory to test skip logic
    (ws / ".hidden_file").write_text("secret")
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    return ws


@pytest.fixture
def chdir_tmp_workspace(tmp_workspace: Path):
    """Change into tmp_workspace for the duration of the test."""
    import os as _os
    old = _os.getcwd()
    _os.chdir(str(tmp_workspace))
    try:
        yield tmp_workspace
    finally:
        _os.chdir(old)


# ---------------------------------------------------------------------------
# Sample files for edit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_py_file(tmp_workspace: Path) -> Path:
    """Create a sample Python file for edit tool tests."""
    content = (
        "def hello():\n"
        "    print('hello world')\n"
        "\n"
        "def goodbye():\n"
        "    print('goodbye world')\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        "        return f'Hello, {name}'\n"
    )
    p = tmp_workspace / "sample.py"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Mock provider config helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_env_api_key(monkeypatch):
    """Set a fake ANTHROPIC_API_KEY in the environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345")
    return "sk-ant-test-key-12345"


# ---------------------------------------------------------------------------
# Subprocess isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def block_real_subprocess(monkeypatch):
    """Prevent any test from accidentally running real git/ripgrep subprocess calls.

    Override subprocess.run globally. Individual tests can undo this by
    explicitly patching the function they need.
    """
    original_run = subprocess.run

    def _safe_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("cmd", [])
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        raise RuntimeError(
            f"Unexpected subprocess.run call in test: {cmd_str}. "
            f"Patch this function explicitly in your test if you need it."
        )

    monkeypatch.setattr(subprocess, "run", _safe_run)
    return original_run
