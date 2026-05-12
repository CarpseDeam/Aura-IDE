"""SQLite/FTS5 database for archival project memory (Tier 2).

Provides on-demand RAG-style retrieval over past dispatch records and
explicitly saved documentation, exposed as Planner-only tools.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class ProjectMemoryDB:
    """SQLite/FTS5-backed archival memory for project context.

    Stores free-text content with optional JSON metadata, and provides
    full-text search via FTS5 (BM25-like ranking).

    Test cases:
        1. Insert + search: Insert a memory, search for a keyword, assert found.
        2. Search with no results: returns empty list.
        3. Delete: Insert then delete, search returns empty.
        4. FTS5 matching across content and metadata fields.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the SQLite database at *db_path*.

        Args:
            db_path: Absolute or workspace-relative path to the .db file.
                     The parent directory will be created if it doesn't exist.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local connection, creating it if necessary."""
        if self._conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._init_tables(conn)
            self._conn = conn
        return self._conn

    @staticmethod
    def _init_tables(conn: sqlite3.Connection) -> None:
        """Create tables and FTS5 virtual table with sync triggers."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, metadata, content=memories, content_rowid=id);

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
            BEGIN
                INSERT INTO memories_fts(rowid, content, metadata)
                VALUES (new.id, new.content, new.metadata);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
            BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, metadata)
                VALUES ('delete', old.id, old.content, old.metadata);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
            BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, metadata)
                VALUES ('delete', old.id, old.content, old.metadata);
                INSERT INTO memories_fts(rowid, content, metadata)
                VALUES (new.id, new.content, new.metadata);
            END;
        """)
        conn.commit()

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Full-text search across memories using FTS5.

        Args:
            query: Natural language search query (FTS5 match syntax).
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: id, content, metadata, created_at.
            Metadata values are deserialised from JSON (or returned as-is if
            not valid JSON).
        """
        if not query or not query.strip():
            return []

        conn = self._get_connection()
        with closing(conn.cursor()) as cur:
            cur.execute(
                """
                SELECT m.id, m.content, m.metadata, m.created_at
                FROM memories m
                JOIN memories_fts f ON m.id = f.rowid
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query.strip(), top_k),
            )
            results = []
            for row in cur.fetchall():
                metadata_raw = row["metadata"]
                if metadata_raw:
                    try:
                        metadata = json.loads(metadata_raw)
                    except (json.JSONDecodeError, TypeError):
                        metadata = metadata_raw
                else:
                    metadata = None
                results.append(
                    {
                        "id": row["id"],
                        "content": row["content"],
                        "metadata": metadata,
                        "created_at": row["created_at"],
                    }
                )
            return results

    def insert(self, content: str, metadata: dict | None = None) -> int:
        """Insert a new memory.

        Args:
            content: The text content to store. Must be non-empty.
            metadata: Optional JSON-serialisable dict for structured data
                      (e.g. ``{"type": "dispatch_record", "goal": "..."}``).

        Returns:
            The rowid of the newly inserted row.

        Raises:
            ValueError: If *content* is empty or not a string.
        """
        if not content or not isinstance(content, str):
            raise ValueError("content must be a non-empty string")

        conn = self._get_connection()
        metadata_json = json.dumps(metadata) if metadata else None
        cur = conn.execute(
            "INSERT INTO memories (content, metadata) VALUES (?, ?)",
            (content.strip(), metadata_json),
        )
        conn.commit()
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("Database insert failed — lastrowid is None")
        return row_id

    def delete(self, memory_id: int) -> bool:
        """Delete a memory by its id.

        Args:
            memory_id: The id of the memory to delete.

        Returns:
            True if a row was deleted, False if no row matched.
        """
        conn = self._get_connection()
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        """Return the total number of stored memories."""
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM memories").fetchone()
        return row["cnt"] if row else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
