"""CodebaseIndex consumes the canonical repository inventory.

Discovery/skip/sensitive-file policy is owned by
``aura.repository_inventory``; ``CodebaseIndex`` must no longer run its own
``os.walk`` and should only apply BM25-specific acceptance (byte cap, binary
sniff) on top of the shared candidate universe.
"""

from __future__ import annotations

from pathlib import Path

from aura.codebase_index.indexer import CodebaseIndex


def _write(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_codebase_index_has_no_independent_walk_implementation() -> None:
    """The old _walk_and_collect os.walk implementation is gone."""
    assert not hasattr(CodebaseIndex, "_walk_and_collect")


def test_codebase_index_excludes_sensitive_files_via_canonical_policy(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def hello(): pass\n")
    _write(tmp_path / ".env", "SECRET=1\n")

    index = CodebaseIndex(tmp_path)
    result = index.search("hello")

    assert result["ok"] is True
    paths = {r["path"] for r in result["results"]}
    assert ".env" not in paths
    assert index.file_count == 1


def test_codebase_index_excludes_canonical_skip_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def hello(): pass\n")
    _write(tmp_path / "node_modules" / "pkg" / "index.js", "function hello() {}\n")

    index = CodebaseIndex(tmp_path)
    index.build()

    assert "app.py" in index._files
    assert not any(p.startswith("node_modules/") for p in index._files)


# ── BM25-specific acceptance still applies on top of canonical discovery ──


def test_bm25_rejects_files_over_its_own_byte_limit(tmp_path: Path) -> None:
    _write(tmp_path / "small.py", "def a(): pass\n")
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 200_000, encoding="utf-8")  # exceeds default 128KB cap

    index = CodebaseIndex(tmp_path)
    index.build()

    assert "small.py" in index._files
    assert "big.py" not in index._files


def test_bm25_rejects_binary_content(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def a(): pass\n")
    binary = tmp_path / "data.bin_but_not_skipped_suffix"
    binary.write_bytes(b"\x00\x01\x02binary junk")

    index = CodebaseIndex(tmp_path)
    index.build()

    assert "app.py" in index._files
    assert "data.bin_but_not_skipped_suffix" not in index._files


def test_bm25_rejects_empty_files(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def a(): pass\n")
    (tmp_path / "empty.py").write_text("", encoding="utf-8")

    index = CodebaseIndex(tmp_path)
    index.build()

    assert "app.py" in index._files
    assert "empty.py" not in index._files


def test_search_reports_indexed_file_count_from_canonical_universe(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "def a(): pass\n")
    _write(tmp_path / "b.py", "def b(): pass\n")

    index = CodebaseIndex(tmp_path)
    result = index.search("a")

    assert result["indexed_file_count"] == 2
    assert result["partial"] is False
