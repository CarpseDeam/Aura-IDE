"""Focused coverage for the ``inspect_code`` production tool.

``inspect_code`` is the one new observation tool this vertical slice adds
(see aura/code_intel/inspection.py for the owner and
aura/conversation/tools/schemas/code_intel.py for the schema). These tests cover
the model-facing contract: truthful resolution/provenance, bounded and
labeled evidence, workspace jailing, effect classification, and graceful
degradation for languages without a rich adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aura.code_intel  # noqa: F401 — triggers adapter registration
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.tools.registry import ToolRegistry

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _inspect(registry: ToolRegistry, **args) -> dict:
    result = registry.execute("inspect_code", args, approval_cb=_APPROVE)
    return result.payload


# ── 1/2/3: catalog membership and effect classification ─────────────────


def test_inspect_code_is_the_only_semantic_inspection_tool_in_production() -> None:
    names = {
        d["function"]["name"]
        for d in ToolCatalog().build_tool_defs(read_only=False)
    }
    assert "inspect_code" in names
    removed = {
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
        "code_intel_audit",
        "read_file_outline",
    }
    assert not (names & removed)
    # search_codebase is a distinct, exposed capability — ranked structural
    # retrieval, not semantic inspection — so it is not part of this removed set.
    assert "search_codebase" in names


def test_inspect_code_is_classified_as_observation() -> None:
    registry = ToolRegistry(workspace_root=Path("."))
    assert registry.tool_effect("inspect_code") is ToolEffect.OBSERVATION


# ── 4: workspace jail ────────────────────────────────────────────────────


def test_inspect_code_rejects_paths_that_escape_the_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    payload = _inspect(registry, path="../outside.py")
    assert payload["ok"] is False
    assert "error" in payload


# ── 5/6: resolvable Python target returns bounded, truthfully-labeled evidence


def test_python_symbol_resolution_returns_bounded_source_and_truthful_target(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(
        tmp_path / "app.py",
        "import os\n\n\ndef helper():\n    return 1\n\n\ndef target_fn(x):\n    return x + 1\n",
    )

    payload = _inspect(registry, path="app.py", symbol="target_fn")

    assert payload["ok"] is True
    target = payload["target"]
    assert target["resolution"] == "exact_symbol_match"
    assert target["name"] == "target_fn"
    assert target["kind"] == "function"
    assert target["declaration_line"] == 8
    assert target["provenance"] == "aura_parser"
    # Python's ast exposes an honest end range for this function.
    assert target["declaration_end_line"] == 9

    excerpt = payload["source_excerpt"]
    assert excerpt["path"] == "app.py"
    assert "def target_fn" in excerpt["text"]
    assert excerpt["provenance"] == "filesystem"
    assert excerpt["parser_bounded"] is True
    assert excerpt["bounded_window"] is False


def test_line_anchor_without_symbol_resolves_by_line(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def foo():\n    pass\n")

    payload = _inspect(registry, path="app.py", line=1)

    assert payload["target"]["resolution"] == "exact_line_match"
    assert payload["target"]["name"] == "foo"


def test_line_between_declarations_is_nearest_preceding_not_exact(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def foo():\n    x = 1\n    return x\n")

    # Line 2 is inside foo's body, not a declaration line itself.
    payload = _inspect(registry, path="app.py", line=2)

    assert payload["target"]["resolution"] == "nearest_preceding_declaration"
    assert payload["target"]["name"] == "foo"


def test_unresolvable_anchor_still_returns_source(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "x = 1\ny = 2\n")

    payload = _inspect(registry, path="app.py", symbol="does_not_exist")

    assert payload["ok"] is True
    assert payload["target"]["resolution"] == "unresolved"
    assert payload["target"]["name"] is None
    assert payload["target"]["provenance"] is None
    assert payload["source_excerpt"]["text"] != ""


# ── 7/8: inspect_code owns local structural evidence only ──────────────


def test_inspect_code_does_not_search_the_workspace_for_occurrences(
    tmp_path: Path, monkeypatch
) -> None:
    """Global lexical discovery belongs to grep_search / search_codebase, not inspect_code."""

    def _guard(*args, **kwargs):
        raise AssertionError("inspect_code must not call find_usages")

    monkeypatch.setattr(
        "aura.conversation.tools.find_usages.find_usages", _guard, raising=False
    )

    registry = ToolRegistry(workspace_root=tmp_path)
    _write(
        tmp_path / "app.py",
        "def shared_name():\n    pass\n",
    )
    _write(
        tmp_path / "other.py",
        "".join(f"shared_name()  # call {i}\n" for i in range(30)),
    )

    payload = _inspect(registry, path="app.py", symbol="shared_name")

    assert payload["ok"] is True
    assert "occurrences" not in payload
    assert "limitations" not in payload
    assert set(payload) == {"ok", "path", "target", "source_excerpt", "diagnostics"}


def test_grep_search_and_search_codebase_remain_the_global_discovery_owners() -> None:
    names = {
        d["function"]["name"]
        for d in ToolCatalog().build_tool_defs(read_only=False)
    }
    assert "grep_search" in names
    assert "search_codebase" in names


# ── 9: graceful degradation for a language without a rich adapter ──────


def test_unsupported_language_still_returns_truthful_local_evidence(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "notes.txt", "line one\nline two\nline three\n")

    payload = _inspect(registry, path="notes.txt", line=2)

    assert payload["ok"] is True
    assert payload["source_excerpt"]["text"] != ""
    assert payload["target"]["language"] in ("text", "unknown")
    assert payload["target"]["declaration_end_line"] is None
    assert payload["source_excerpt"]["parser_bounded"] is False
    assert payload["source_excerpt"]["bounded_window"] is True


# ── 11/12/13/14: parser-owned declaration ranges ─────────────────────────


def test_resolved_python_target_reports_exact_parser_owned_range(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(
        tmp_path / "app.py",
        "def target_fn(x):\n    y = x + 1\n    return y\n",
    )

    payload = _inspect(registry, path="app.py", symbol="target_fn")

    target = payload["target"]
    assert target["declaration_line"] == 1
    assert target["declaration_end_line"] == 3
    assert target["declaration_end_column"] == len("    return y")
    # An exact range is now known and truthfully reflected in the excerpt facts.
    assert payload["source_excerpt"]["parser_bounded"] is True


def test_inspect_code_uses_parser_bounded_source_when_exact_range_known(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(
        tmp_path / "app.py",
        "x = 0\n\n\ndef target_fn(x):\n    y = x + 1\n    return y\n\n\nz = 1\n",
    )

    payload = _inspect(registry, path="app.py", symbol="target_fn")

    excerpt = payload["source_excerpt"]
    assert excerpt["parser_bounded"] is True
    assert excerpt["bounded_window"] is False
    assert excerpt["start_line"] == 4
    assert excerpt["end_line"] == 6
    assert excerpt["text"] == "def target_fn(x):\n    y = x + 1\n    return y"
    # No surrounding context lines (x = 0 / z = 1) leak into a parser-bounded
    # excerpt — it is exactly the declaration, not a heuristic window.
    assert "x = 0" not in excerpt["text"]
    assert "z = 1" not in excerpt["text"]
    assert excerpt["truncated"] is False


def test_parser_owned_body_larger_than_cap_is_bounded_but_range_metadata_stays_true(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    from aura.code_intel.inspection import EXCERPT_MAX_LINES

    body_lines = "\n".join(f"    x{i} = {i}" for i in range(EXCERPT_MAX_LINES + 20))
    _write(tmp_path / "app.py", f"def big_fn():\n{body_lines}\n    return 1\n")

    payload = _inspect(registry, path="app.py", symbol="big_fn")

    target = payload["target"]
    full_end_line = EXCERPT_MAX_LINES + 20 + 2  # def line + body + return line
    # Full structural end range is preserved in target metadata even though
    # the excerpt itself had to be cut short.
    assert target["declaration_end_line"] == full_end_line

    excerpt = payload["source_excerpt"]
    assert excerpt["parser_bounded"] is True
    assert excerpt["end_line"] < full_end_line
    assert excerpt["truncated"] is True


def test_fallback_text_target_does_not_claim_a_body_range(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "notes.txt", "line one\nline two\nline three\n")

    payload = _inspect(registry, path="notes.txt", line=1)

    target = payload["target"]
    assert target["declaration_end_line"] is None
    assert target["declaration_end_column"] is None

    excerpt = payload["source_excerpt"]
    assert excerpt["parser_bounded"] is False
    assert excerpt["bounded_window"] is True


# ── 10: workspace-root change re-binds the inspector ────────────────────


def test_workspace_root_change_rebinds_inspection(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a / "app.py", "def only_in_a():\n    pass\n")
    _write(root_b / "app.py", "def only_in_b():\n    pass\n")

    registry = ToolRegistry(workspace_root=root_a)
    first = _inspect(registry, path="app.py", line=1)
    assert first["target"]["name"] == "only_in_a"

    registry.set_workspace_root(root_b)
    second = _inspect(registry, path="app.py", line=1)
    assert second["target"]["name"] == "only_in_b"


# ── CodeIntel ownership lifecycle: targeted freshness, no full walk ─────


def test_inspect_code_never_triggers_a_full_workspace_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    from aura.code_intel.index import CodeIntelIndex

    def _refresh_guard(self, changed_files=None):
        raise AssertionError("inspect_code must not trigger CodeIntelIndex.refresh()")

    monkeypatch.setattr(CodeIntelIndex, "refresh", _refresh_guard)

    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def target_fn():\n    return 1\n")

    payload = _inspect(registry, path="app.py", symbol="target_fn")

    assert payload["ok"] is True
    assert payload["target"]["name"] == "target_fn"


def test_first_inspection_indexes_only_the_requested_file(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def a(): pass\n")
    _write(tmp_path / "sibling.py", "def b(): pass\n")

    _inspect(registry, path="app.py", line=1)

    assert registry._code_intel_index.file_paths() == ["app.py"]


def test_reinspecting_unchanged_file_does_not_reparse(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def stable(): pass\n")

    _inspect(registry, path="app.py", line=1)
    first_info = registry._code_intel_index.get_file("app.py")

    _inspect(registry, path="app.py", line=1)
    second_info = registry._code_intel_index.get_file("app.py")

    # Identity, not just equality: proves ensure_fresh's stat-based skip
    # gate returned without re-parsing and re-creating the FileInfo record.
    assert second_info is first_info


def test_editing_inspected_file_returns_new_symbol(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    app_py = tmp_path / "app.py"
    _write(app_py, "def old_fn(): pass\n")

    _inspect(registry, path="app.py", line=1)

    _write(app_py, "def new_fn():\n    return 2\n")
    payload = _inspect(registry, path="app.py", line=1)

    assert payload["target"]["name"] == "new_fn"


def test_workspace_root_change_discards_old_index_facts(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a / "app.py", "def only_in_a(): pass\n")
    _write(root_b / "app.py", "def only_in_b(): pass\n")

    registry = ToolRegistry(workspace_root=root_a)
    _inspect(registry, path="app.py", line=1)
    old_index = registry._code_intel_index
    assert "app.py" in old_index.file_paths()

    registry.set_workspace_root(root_b)

    # A brand new index is bound — the old one (and its "only_in_a" fact)
    # is no longer reachable from the registry at all.
    assert registry._code_intel_index is not old_index
    assert registry._code_inspector._index is registry._code_intel_index
    assert registry._code_intel_index.file_paths() == []

    _inspect(registry, path="app.py", line=1)
    names = [s.name for s in registry._code_intel_index.get_symbols("app.py")]
    assert names == ["only_in_b"]


# ── withheld code-intel handlers: freshness policy per operation ────────


def test_code_intel_outline_uses_targeted_freshness_not_full_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    from aura.code_intel.index import CodeIntelIndex

    def _refresh_guard(self, changed_files=None):
        raise AssertionError("code_intel_outline must not trigger a full refresh")

    monkeypatch.setattr(CodeIntelIndex, "refresh", _refresh_guard)

    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def helper():\n    pass\n")

    result = registry.execute("code_intel_outline", {"path": "app.py"}, approval_cb=_APPROVE)

    assert result.payload["ok"] is True
    assert any(f["name"] == "helper" for f in result.payload["outline"]["functions"])


def test_code_intel_references_still_uses_full_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    from aura.code_intel.index import CodeIntelIndex

    calls: list[Any] = []
    original_refresh = CodeIntelIndex.refresh

    def _tracking_refresh(self, changed_files=None):
        calls.append(changed_files)
        return original_refresh(self, changed_files)

    monkeypatch.setattr(CodeIntelIndex, "refresh", _tracking_refresh)

    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def target():\n    pass\n")

    result = registry.execute(
        "code_intel_references", {"symbol": "target"}, approval_cb=_APPROVE
    )

    assert result.payload["ok"] is True
    assert calls, "code_intel_references must call CodeIntelIndex.refresh()"


def test_code_intel_dependents_still_uses_full_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    from aura.code_intel.index import CodeIntelIndex

    calls: list[Any] = []
    original_refresh = CodeIntelIndex.refresh

    def _tracking_refresh(self, changed_files=None):
        calls.append(changed_files)
        return original_refresh(self, changed_files)

    monkeypatch.setattr(CodeIntelIndex, "refresh", _tracking_refresh)

    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "lib.py", "VERSION = 1\n")
    _write(tmp_path / "app.py", "import lib\n")

    result = registry.execute(
        "code_intel_dependents", {"path": "lib.py"}, approval_cb=_APPROVE
    )

    assert result.payload["ok"] is True
    assert calls, "code_intel_dependents must call CodeIntelIndex.refresh()"


def test_code_intel_audit_reuses_registry_owned_index(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def a():\n    pass\n")

    result = registry.execute(
        "code_intel_audit", {"paths": ["app.py"]}, approval_cb=_APPROVE
    )

    assert result.payload["ok"] is True
    # The audit was passed the registry's own index rather than a private
    # one it fetched from a global cache.
    assert "app.py" in registry._code_intel_index.file_paths()
