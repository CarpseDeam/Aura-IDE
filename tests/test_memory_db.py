"""Unit tests for ``aura.memory_db.ProjectMemoryDB``."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aura.memory_db import ProjectMemoryDB


@pytest.fixture
def db() -> ProjectMemoryDB:
    """Create a temporary ProjectMemoryDB for each test."""
    tmp_dir = Path(tempfile.mkdtemp())
    db_path = tmp_dir / "test_memory.db"
    pdb = ProjectMemoryDB(db_path)
    yield pdb
    # Cleanup: explicitly close the connection so the temp db can be deleted
    pdb.close()
    db_path.unlink(missing_ok=True)
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


class TestProjectMemoryDB:
    """Test suite for ProjectMemoryDB."""

    def test_insert_and_search(self, db: ProjectMemoryDB) -> None:
        """Insert a memory, search for a keyword, assert it is found."""
        db.insert("The quick brown fox jumps over the lazy dog.")
        results = db.search("fox")
        assert len(results) > 0
        assert "fox" in results[0]["content"]

    def test_search_no_results(self, db: ProjectMemoryDB) -> None:
        """Search with no matching results returns empty list."""
        db.insert("Alpha beta gamma.")
        results = db.search("zzz_notfound")
        assert results == []

    def test_delete(self, db: ProjectMemoryDB) -> None:
        """Insert then delete a memory; search returns empty."""
        mid = db.insert("Delete me please.")
        assert db.count() == 1
        deleted = db.delete(mid)
        assert deleted is True
        assert db.count() == 0
        results = db.search("delete")
        assert results == []

    def test_delete_nonexistent(self, db: ProjectMemoryDB) -> None:
        """Deleting a non-existent id returns False."""
        result = db.delete(99999)
        assert result is False

    def test_count(self, db: ProjectMemoryDB) -> None:
        """Count returns the correct number of memories."""
        assert db.count() == 0
        db.insert("First")
        assert db.count() == 1
        db.insert("Second")
        assert db.count() == 2

    def test_fts5_metadata_search(self, db: ProjectMemoryDB) -> None:
        """FTS5 should match across both content and metadata fields."""
        db.insert(
            "Authentication middleware refactored.",
            metadata={"type": "dispatch_record", "goal": "Refactor auth", "tags": ["auth"]},
        )
        # Search by metadata content (the json string is part of the FTS index)
        results = db.search("dispatch_record")
        assert len(results) > 0
        assert results[0]["metadata"]["type"] == "dispatch_record"

    def test_insert_empty_content_raises(self, db: ProjectMemoryDB) -> None:
        """Inserting empty content raises ValueError."""
        with pytest.raises(ValueError, match="content must be a non-empty string"):
            db.insert("")

    def test_insert_with_metadata(self, db: ProjectMemoryDB) -> None:
        """Insert and retrieve a memory with metadata."""
        meta = {"type": "note", "tags": ["important"]}
        mid = db.insert("Important note content.", metadata=meta)
        results = db.search("note")
        assert len(results) > 0
        r = results[0]
        assert r["id"] == mid
        assert r["metadata"] == meta

    def test_autocreate_directory(self) -> None:
        """ProjectMemoryDB creates parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nonexistent" / "subdir" / "mem.db"
            pdb = ProjectMemoryDB(nested)
            pdb.insert("Auto-create test.")
            assert nested.exists()
            results = pdb.search("Auto")
            assert len(results) == 1
            pdb.close()
