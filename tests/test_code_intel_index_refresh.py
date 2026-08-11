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


class TestCodeIntelIndexEnsureFresh:
    """Targeted single-file freshness — never a workspace walk."""

    def test_first_inspection_of_unseen_file_indexes_just_that_file(
        self, tmp_path: Path
    ) -> None:
        index = CodeIntelIndex(tmp_path)
        _write(tmp_path / "app.py", "def only_fn(): pass\n")
        _write(tmp_path / "other.py", "def other_fn(): pass\n")

        index.ensure_fresh("app.py")

        assert "app.py" in index.file_paths()
        assert "other.py" not in index.file_paths()
        names = [s.name for s in index.get_symbols("app.py")]
        assert names == ["only_fn"]

    def test_reinspecting_unchanged_file_does_not_reparse(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)
        _write(tmp_path / "app.py", "def stable_fn(): pass\n")

        index.ensure_fresh("app.py")
        first_info = index.get_file("app.py")
        assert first_info is not None

        index.ensure_fresh("app.py")
        second_info = index.get_file("app.py")

        # Same FileInfo instance: ensure_fresh returned after the stat-based
        # skip gate without ever re-parsing and re-creating the record.
        assert second_info is first_info

    def test_editing_inspected_file_reindexes_it(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)
        app_py = tmp_path / "app.py"
        _write(app_py, "def old_fn(): pass\n")
        index.ensure_fresh("app.py")
        assert [s.name for s in index.get_symbols("app.py")] == ["old_fn"]

        _write(app_py, "def brand_new_fn():\n    return 42\n")
        index.ensure_fresh("app.py")

        names = [s.name for s in index.get_symbols("app.py")]
        assert names == ["brand_new_fn"]
        assert "old_fn" not in names

    def test_deleting_inspected_file_evicts_it(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)
        app_py = tmp_path / "app.py"
        _write(app_py, "def gone_soon(): pass\n")
        index.ensure_fresh("app.py")
        assert index.get_file("app.py") is not None

        app_py.unlink()
        index.ensure_fresh("app.py")

        assert index.get_file("app.py") is None
        assert index.get_symbols("app.py") == []

    def test_ensure_fresh_never_touches_other_files(self, tmp_path: Path) -> None:
        index = CodeIntelIndex(tmp_path)
        _write(tmp_path / "app.py", "def a(): pass\n")
        _write(tmp_path / "sibling.py", "def b(): pass\n")

        index.ensure_fresh("app.py")
        index.ensure_fresh("app.py")

        assert index.file_paths() == ["app.py"]

    def test_ensure_fresh_evicts_nonexistent_path_without_error(
        self, tmp_path: Path
    ) -> None:
        index = CodeIntelIndex(tmp_path)
        # Never indexed and never existed — must be a no-op, not an error.
        index.ensure_fresh("never_existed.py")
        assert index.get_file("never_existed.py") is None
