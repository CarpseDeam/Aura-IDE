from __future__ import annotations

import builtins
import importlib
from types import SimpleNamespace

from aura.conversation import dependency_setup
from aura.conversation.dependency_setup import (
    declared_dependencies,
    safe_project_environment_setup_command,
)


def test_pyproject_dependencies_parse_with_tomli_fallback(tmp_path, monkeypatch) -> None:
    original_import = builtins.__import__
    fake_tomli = SimpleNamespace(loads=dependency_setup.tomllib.loads)

    def import_without_tomllib(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tomllib":
            raise ModuleNotFoundError("tomllib")
        if name == "tomli":
            return fake_tomli
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "__import__", import_without_tomllib)
        reloaded = importlib.reload(dependency_setup)
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\ndependencies = ['fastapi>=0.1']\n",
            encoding="utf-8",
        )

        assert reloaded.tomllib is fake_tomli
        assert "fastapi" in reloaded.declared_dependencies(tmp_path)

    importlib.reload(dependency_setup)


def test_project_manager_setup_requires_project_evidence(tmp_path) -> None:
    assert safe_project_environment_setup_command("uv sync", workspace_root=tmp_path) is False
    assert safe_project_environment_setup_command("poetry install", workspace_root=tmp_path) is False
    assert safe_project_environment_setup_command("pdm install", workspace_root=tmp_path) is False

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    assert safe_project_environment_setup_command("uv sync", workspace_root=tmp_path) is True
    assert safe_project_environment_setup_command("poetry install", workspace_root=tmp_path) is True
    assert safe_project_environment_setup_command("pdm install", workspace_root=tmp_path) is True
    assert "demo" not in declared_dependencies(tmp_path)
