"""CodeIntelIndex consumes the canonical repository inventory.

``_refresh_full`` used to run its own bounded ``os.walk``; discovery is now
owned by ``aura.repository_inventory``. These tests pin: no independent walk
implementation remains, a partial inventory must not cause eviction of
unseen-but-still-real cached files, and ``ensure_fresh`` stays a targeted
single-file operation that never triggers a full inventory scan.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import aura.code_intel  # noqa: F401 — triggers adapter registration
from aura.code_intel.index import CodeIntelIndex
from aura.repository_inventory import RepositoryFile, RepositoryInventory


def _write(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_code_intel_index_has_no_independent_walk_implementation() -> None:
    """The old os.walk-based full-workspace discovery is gone."""
    import inspect

    source = inspect.getsource(CodeIntelIndex._refresh_full)
    assert "os.walk" not in source


def test_full_refresh_consumes_canonical_inventory(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def hello(): pass\n")
    _write(tmp_path / ".env", "SECRET=1\n")  # must never be indexed

    index = CodeIntelIndex(tmp_path)
    index.refresh()

    assert "app.py" in index.file_paths()
    assert ".env" not in index.file_paths()


def test_partial_inventory_does_not_evict_unseen_cached_files(tmp_path: Path) -> None:
    app_py = tmp_path / "app.py"
    _write(app_py, "def hello(): pass\n")

    index = CodeIntelIndex(tmp_path)
    index.refresh()
    assert "app.py" in index.file_paths()

    partial_inventory = RepositoryInventory(
        root=tmp_path.resolve(),
        files=(),  # nothing "seen" this pass
        source="filesystem",
        complete=False,
        incomplete_reason="file cap reached",
    )
    with patch("aura.code_intel.index.build_inventory", return_value=partial_inventory):
        index.refresh()

    # app.py was not re-seen, but the inventory admitted it was incomplete —
    # the cached fact must survive rather than being treated as deleted.
    assert "app.py" in index.file_paths()


def test_complete_inventory_still_evicts_genuinely_deleted_files(tmp_path: Path) -> None:
    app_py = tmp_path / "app.py"
    _write(app_py, "def hello(): pass\n")

    index = CodeIntelIndex(tmp_path)
    index.refresh()
    assert "app.py" in index.file_paths()

    app_py.unlink()
    index.refresh()

    assert "app.py" not in index.file_paths()


def test_ensure_fresh_never_calls_build_inventory(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def target(): pass\n")
    index = CodeIntelIndex(tmp_path)

    with patch("aura.code_intel.index.build_inventory") as mock_build:
        index.ensure_fresh("app.py")

    mock_build.assert_not_called()
    assert "app.py" in index.file_paths()


def test_ensure_fresh_evicts_a_path_rejected_by_canonical_policy(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    _write(secret, "SECRET=1\n")
    index = CodeIntelIndex(tmp_path)

    index.ensure_fresh(".env")

    assert index.get_file(".env") is None
