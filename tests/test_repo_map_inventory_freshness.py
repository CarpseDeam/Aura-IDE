"""repo_map cache freshness is driven by the canonical repository inventory.

The old check only considered ``.py``/``.ts``/``.tsx``/``.js`` mtimes, so
adding, removing, or changing a file in any other supported repository
language never invalidated the cache. Freshness must now track the whole
canonical candidate universe.
"""

from __future__ import annotations

import time
from pathlib import Path

import aura.code_intel  # noqa: F401 — triggers adapter registration
from aura.repo_map import generate_repo_map, generate_repo_summary


def _write(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repo_summary_invalidates_on_a_non_python_js_language_file(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def hello(): pass\n")
    first = generate_repo_summary(tmp_path, force=True)

    time.sleep(0.02)
    # Rust has no adapter dependency on py/js/ts, and the old get_max_mtime
    # extension allowlist never considered it — the fingerprint must still
    # notice the file count changed.
    _write(tmp_path / "lib.rs", "fn main() {}\n")
    second = generate_repo_summary(tmp_path, force=True)

    assert first != second
    assert "2 indexed source files" in second


def test_repo_map_invalidates_on_a_non_python_js_language_file(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def hello(): pass\n")
    first = generate_repo_map(tmp_path, force=True)

    time.sleep(0.02)
    _write(tmp_path / "lib.go", "package main\nfunc main() {}\n")
    second = generate_repo_map(tmp_path, force=True)

    assert first != second


def test_repo_summary_invalidates_on_file_removal(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    _write(a, "def a(): pass\n")
    _write(tmp_path / "b.py", "def b(): pass\n")
    first = generate_repo_summary(tmp_path, force=True)

    time.sleep(0.02)
    a.unlink()
    second = generate_repo_summary(tmp_path, force=True)

    assert first != second
    assert "1 indexed source files" in second
