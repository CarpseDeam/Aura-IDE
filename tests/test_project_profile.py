from __future__ import annotations

import os

import pytest

from aura.conversation.project_profile import detect_project_profile

# Platform helpers

def _venv_executable(tmp_path) -> str:
    """Create a .venv directory with a python executable and return its relative path."""
    venv = tmp_path / ".venv"
    if os.name == "nt":
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_text("fake")
        return ".venv/Scripts/python.exe"
    else:
        bindir = venv / "bin"
        bindir.mkdir(parents=True)
        (bindir / "python").write_text("fake")
        return ".venv/bin/python"

# Tests

class TestPythonPyprojectTomlWithVenv:
    """Python pyproject.toml with PEP 621 deps + .venv."""

    def test_detect(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""\
[project]
name = "test-project"
dependencies = ["fastapi", "pydantic>=2.0", "httpx"]
""")
        exec_path = _venv_executable(tmp_path)

        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ("python",)
        assert profile.has_venv is True
        assert profile.python_executable == exec_path
        assert profile.package_manager == "pip"
        assert profile.setup_command is not None
        assert "pyproject.toml" in profile.manifests

class TestPythonPyprojectTomlWithUvLock:
    """Python pyproject.toml + uv.lock → uv package manager."""

    def test_detect(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""\
[project]
name = "test-project"
dependencies = ["fastapi"]
""")
        (tmp_path / "uv.lock").write_text("")

        profile = detect_project_profile(tmp_path)

        assert profile.package_manager == "uv"
        assert profile.setup_command == "uv sync"
        assert "uv.lock" in profile.lockfiles

class TestPythonRequirementsTxtWithoutVenv:
    """requirements.txt with deps, no .venv."""

    def test_detect(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("""\
fastapi==0.100.0
pydantic>=2.0
httpx
""")

        profile = detect_project_profile(tmp_path)

        assert profile.has_venv is False
        assert profile.package_manager == "pip"
        assert "fastapi" in profile.declared_dependencies
        assert "pydantic" in profile.declared_dependencies
        assert "httpx" in profile.declared_dependencies
        assert profile.project_types == ("python",)

class TestPythonPyprojectTomlWithPoetry:
    """pyproject.toml with [tool.poetry] → poetry package manager."""

    def test_detect(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""\
[tool.poetry]
name = "test-project"

[tool.poetry.dependencies]
requests = "^2.28"

[tool.poetry.dev-dependencies]
pytest = "^7.0"
""")

        profile = detect_project_profile(tmp_path)

        assert profile.package_manager == "poetry"
        assert "requests" in profile.declared_dependencies
        assert "pytest" in profile.declared_dependencies

class TestNodePackageJson:
    """package.json with dependencies and scripts."""

    def test_detect(self, tmp_path):
        (tmp_path / "package.json").write_text("""\
{
  "name": "test-project",
  "dependencies": {
    "express": "^4.18.0",
    "lodash": "^4.17.21"
  },
  "devDependencies": {
    "jest": "^29.0.0"
  },
  "scripts": {
    "test": "jest",
    "build": "tsc"
  }
}
""")

        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ("node",)
        assert profile.package_manager == "npm"
        assert len(profile.node_scripts) == 2
        assert "test" in dict(profile.node_scripts)
        assert "build" in dict(profile.node_scripts)
        assert any("npm" in cmd for cmd in profile.validation_commands)
        assert "express" in profile.declared_dependencies
        assert "lodash" in profile.declared_dependencies
        assert "jest" in profile.declared_dependencies

class TestRustCargoToml:
    """Cargo.toml → Rust project."""

    def test_detect(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("""\
[package]
name = "test-project"
version = "0.1.0"
""")

        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ("rust",)
        assert profile.package_manager == "cargo"
        assert profile.setup_command == "cargo fetch"

class TestGoGoMod:
    """go.mod → Go project."""

    def test_detect(self, tmp_path):
        (tmp_path / "go.mod").write_text("""\
module example.com/test-project

go 1.21
""")

        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ("go",)
        assert profile.package_manager == "go"
        assert profile.setup_command == "go mod download"

class TestMixedPythonAndNode:
    """pyproject.toml + package.json → both types detected."""

    def test_detect(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""\
[project]
name = "test-project"
dependencies = ["fastapi"]
""")
        (tmp_path / "package.json").write_text("""\
{
  "name": "test-project",
  "dependencies": {
    "express": "^4.18.0"
  }
}
""")

        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ("node", "python")
        assert "pyproject.toml" in profile.manifests
        assert "package.json" in profile.manifests

class TestEmptyDirectory:
    """No project files → empty profile."""

    def test_detect(self, tmp_path):
        profile = detect_project_profile(tmp_path)

        assert profile.project_types == ()
        assert profile.manifests == ()
        assert profile.lockfiles == ()
        assert profile.package_manager is None
        assert profile.has_venv is False

class TestFileNotFoundError:
    """Non-existent path raises FileNotFoundError."""

    def test_detect(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        with pytest.raises(FileNotFoundError):
            detect_project_profile(nonexistent)

class TestSummarize:
    """summarize() output contains expected key terms."""

    def test_output(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""\
[project]
name = "test-project"
dependencies = ["fastapi", "httpx"]
""")
        _venv_executable(tmp_path)

        profile = detect_project_profile(tmp_path)
        summary = profile.summarize()

        assert "Project types:" in summary
        assert "python" in summary
        assert "Manifests:" in summary
        assert "pyproject.toml" in summary
        assert "Package manager:" in summary
        assert "Virtual env:" in summary
        assert "Setup:" in summary
        assert "fastapi" in summary
        assert "httpx" in summary
