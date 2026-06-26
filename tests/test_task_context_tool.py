from __future__ import annotations

from pathlib import Path

from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.task_context import (
    _MAX_QUERY_HITS,
    _MAX_SYMBOL_HITS_PER_SYMBOL,
    _iter_text_candidates,
    read_task_context,
)


def test_files_mode_returns_paths_and_snippets(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "alpha.py").write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")
    (ws / "beta.py").write_text("def beta():\n    return alpha()\n", encoding="utf-8")

    payload = read_task_context(ws, {"files": ["alpha.py"], "include_dependents": False})

    assert payload["ok"] is True
    assert payload["files"] == ["alpha.py"]
    assert "File: alpha.py" in payload["context"]
    assert "def alpha" in payload["context"]


def test_query_mode_returns_bounded_relevant_hits(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "alpha.py").write_text("needle = 'first'\n", encoding="utf-8")
    (ws / "beta.py").write_text("value = 'needle second'\n", encoding="utf-8")

    payload = read_task_context(ws, {"query": "needle", "max_chars": 500})

    assert payload["ok"] is True
    assert "Query Hits" in payload["context"]
    assert "alpha.py:1" in payload["context"]
    assert "needle" in payload["context"]
    assert len(payload["context"]) <= 500


def test_missing_file_returns_caveat(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()

    payload = read_task_context(ws, {"files": ["missing.py"]})

    assert payload["ok"] is True
    assert payload["files"] == ["missing.py"]
    assert any("missing.py" in caveat for caveat in payload["caveats"])
    assert "(file not found)" in payload["context"]


def test_max_chars_cap_works(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "alpha.py").write_text("x = '" + ("a" * 500) + "'\n", encoding="utf-8")

    payload = read_task_context(ws, {"files": ["alpha.py"], "max_chars": 80})

    assert len(payload["context"]) <= 80
    assert payload["truncated"] is True


def test_registry_exposes_read_task_context_in_worker_mode(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    registry = ToolRegistry(ws, mode="worker")

    tool_names = {tool["function"]["name"] for tool in registry.tool_defs()}

    assert "read_task_context" in tool_names


def test_registry_executes_read_task_context(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    registry = ToolRegistry(ws, mode="worker")

    result = registry.execute("read_task_context", {"files": ["alpha.py"]}, lambda _: True)

    assert result.ok is True
    assert result.payload["ok"] is True
    assert "alpha.py" in result.payload["context"]


def test_bounded_walker_skips_hidden_skip_dirs_and_suffixes(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "visible.py").write_text("ok\n", encoding="utf-8")
    (ws / "binary.pyc").write_bytes(b"skip")
    hidden = ws / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("skip\n", encoding="utf-8")
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("skip\n", encoding="utf-8")
    cache_dir = ws / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "cached.py").write_text("skip\n", encoding="utf-8")

    paths = [path.relative_to(ws).as_posix() for path in _iter_text_candidates(ws)]

    assert paths == ["visible.py"]


def test_bounded_walker_respects_max_files(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (ws / name).write_text("needle\n", encoding="utf-8")

    paths = [path.relative_to(ws).as_posix() for path in _iter_text_candidates(ws, max_files=2)]

    assert paths == ["a.py", "b.py"]


def test_query_mode_does_not_use_rglob(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "alpha.py").write_text("needle\n", encoding="utf-8")

    def fail_rglob(self, pattern):
        raise AssertionError("rglob should not be used")

    monkeypatch.setattr(Path, "rglob", fail_rglob)

    payload = read_task_context(ws, {"query": "needle"})

    assert payload["ok"] is True
    assert "alpha.py:1" in payload["context"]


def test_query_mode_stops_at_hit_budget(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    for index in range(_MAX_QUERY_HITS + 10):
        (ws / f"hit_{index:02}.py").write_text("needle\n", encoding="utf-8")

    payload = read_task_context(ws, {"query": "needle", "max_chars": 10000})
    hit_lines = [line for line in payload["context"].splitlines() if ".py:1:" in line]

    assert len(hit_lines) == _MAX_QUERY_HITS
    assert any("Query hits were truncated." == caveat for caveat in payload["caveats"])


def test_symbol_mode_returns_bounded_hits(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    for index in range(_MAX_SYMBOL_HITS_PER_SYMBOL + 8):
        (ws / f"use_{index:02}.py").write_text("target_symbol()\n", encoding="utf-8")

    payload = read_task_context(ws, {"symbols": ["target_symbol"], "max_chars": 10000})
    hit_lines = [line for line in payload["context"].splitlines() if "target_symbol()" in line]

    assert len(hit_lines) == _MAX_SYMBOL_HITS_PER_SYMBOL
    assert any("Symbol hits were truncated." == caveat for caveat in payload["caveats"])
