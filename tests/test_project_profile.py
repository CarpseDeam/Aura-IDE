from __future__ import annotations

from pathlib import Path

from aura.conversation.project_profile import detect_project_profile


class TestProjectProfileValidationCommands:
    """Regression tests: non-Python projects must NOT receive py_compile validation."""

    def test_rust_only_project_gets_no_python_validation(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = \"test\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
        )
        profile = detect_project_profile(tmp_path)

        assert "rust" in profile.project_types
        assert "python" not in profile.project_types
        assert not any("py_compile" in cmd for cmd in profile.validation_commands)
        assert "python" not in " ".join(profile.validation_commands).lower()

    def test_node_only_project_gets_no_python_validation(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"name": "test", "scripts": {"test": "echo test"}}\n'
        )
        profile = detect_project_profile(tmp_path)

        assert "node" in profile.project_types
        assert "python" not in profile.project_types
        assert not any("py_compile" in cmd for cmd in profile.validation_commands)

    def test_go_only_project_gets_no_python_validation(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module test\ngo 1.21\n")
        profile = detect_project_profile(tmp_path)

        assert "go" in profile.project_types
        assert "python" not in profile.project_types
        assert not any("py_compile" in cmd for cmd in profile.validation_commands)

    def test_python_project_still_gets_py_compile(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"test\"\nversion = \"0.1.0\"\n"
        )
        profile = detect_project_profile(tmp_path)

        assert "python" in profile.project_types
        assert any("py_compile" in cmd for cmd in profile.validation_commands)

    def test_mixed_project_gets_both(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"test\"\nversion = \"0.1.0\"\n"
        )
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = \"test\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
        )
        profile = detect_project_profile(tmp_path)

        assert "python" in profile.project_types
        assert "rust" in profile.project_types
        assert any("py_compile" in cmd for cmd in profile.validation_commands)
        assert any("cargo" in cmd for cmd in profile.validation_commands)
