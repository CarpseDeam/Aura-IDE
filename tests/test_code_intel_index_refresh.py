from __future__ import annotations

from pathlib import Path

import aura.code_intel  # noqa: F401 — triggers adapter registration
from aura.code_intel.index import CodeIntelIndex


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestCodeIntelIndexRefresh:
    """Incremental refresh for CodeIntelIndex."""

    def test_incremental_refresh_replaces_old_symbols(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)

        app_py = tmp_path / "app.py"
        _write(app_py, "def old_name(): pass\n")
        index.refresh()

        names = [s.name for s in index.get_symbols("app.py")]
        assert "old_name" in names

        _write(app_py, "def new_name(): pass\n")
        index.refresh(changed_files=["app.py"])

        names = [s.name for s in index.get_symbols("app.py")]
        assert "new_name" in names
        assert "old_name" not in names

    def test_incremental_refresh_removes_deleted_files(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)

        app_py = tmp_path / "app.py"
        _write(app_py, "x = 1\n")
        index.refresh()

        assert index.get_file("app.py") is not None

        app_py.unlink()
        index.refresh(changed_files=["app.py"])

        assert index.get_file("app.py") is None
        assert index.get_symbols("app.py") == []

    def test_dependency_reverse_edges_cleaned(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)

        lib_py = tmp_path / "lib.py"
        _write(lib_py, "VERSION = 1\n")

        app_py = tmp_path / "app.py"
        _write(app_py, "import lib\n")

        index.refresh()

        assert "app.py" in index.get_dependents("lib.py")

        _write(app_py, "x = 42\n")
        index.refresh(changed_files=["app.py"])

        assert "app.py" not in index.get_dependents("lib.py")

    def test_full_refresh_evicts_deleted_files(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)

        app_py = tmp_path / "app.py"
        _write(app_py, "z = 0\n")
        index.refresh()

        assert "app.py" in index.file_paths()

        app_py.unlink()
        index.refresh()

        assert "app.py" not in index.file_paths()
