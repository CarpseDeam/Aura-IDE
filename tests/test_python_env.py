from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.registry import ToolRegistry
from aura.python_env import (
    build_project_python_command,
    build_project_tool_command,
    detect_project_python_env,
)
from aura.project_env import build_project_command, detect_project_toolchains


def test_detect_project_python_env_prefers_dot_venv_scripts(tmp_path: Path) -> None:
    dot_venv = tmp_path / ".venv" / "Scripts"
    plain_venv = tmp_path / "venv" / "Scripts"
    plain_venv.mkdir(parents=True)
    dot_venv.mkdir(parents=True)
    (plain_venv / "python.exe").write_text("", encoding="utf-8")
    expected = dot_venv / "python.exe"
    expected.write_text("", encoding="utf-8")

    env = detect_project_python_env(tmp_path)

    assert env.python == expected


def test_py_compile_command_rewrites_to_project_venv(tmp_path: Path) -> None:
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    plan = build_project_python_command(
        tmp_path,
        "python -m py_compile aura/config.py",
    )

    assert str(python) in plan.command
    assert "-m py_compile aura/config.py" in plan.command


def test_pytest_in_python_project_without_project_venv_is_environment_setup_needed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    plan = build_project_tool_command(tmp_path, "pytest tests/test_x.py")

    assert plan.missing_dependency == "pytest"
    assert plan.command == "pytest tests/test_x.py"


def test_non_python_project_does_not_detect_python_toolchain(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")
    (tmp_path / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")

    assert detect_project_toolchains(tmp_path) == []


def test_non_python_project_missing_pytest_is_generic_tool_caveat(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")

    with patch("aura.project_env.shutil_which", return_value=None):
        plan = build_project_command(tmp_path, "pytest tests/test_x.py")

    assert plan.toolchain is None
    assert plan.missing_tool == "pytest"
    assert plan.failure_class == "project_environment_missing_tool"


def test_non_python_project_does_not_rewrite_npm_validation(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node test.js"}}\n', encoding="utf-8")

    with patch("aura.project_env.shutil_which", return_value="npm"):
        plan = build_project_command(tmp_path, "npm test")

    assert plan.command == "npm test"
    assert plan.toolchain is None
    assert plan.missing_tool is None


@pytest.mark.skip(reason="focused py_compile hook was removed from ConversationManager")
def test_focused_py_compile_uses_project_venv(tmp_path: Path) -> None:
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    target = tmp_path / "aura" / "config.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")
    tools = MagicMock(spec=ToolRegistry)
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    manager = ConversationManager(History(), tools)

    with patch("aura.conversation.manager.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, diagnostics = manager._run_focused_py_compile(["aura/config.py"])

    assert ok is True
    assert diagnostics == "aura/config.py: ok"
    assert run.call_args.args[0][0] == str(python)
