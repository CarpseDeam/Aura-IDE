"""Focused coverage for the ``inspect_code`` production tool.

``inspect_code`` is the one new observation tool this vertical slice adds
(see aura/code_intel/inspection.py for the owner and
aura/conversation/tools/_inspect_schema.py for the schema). These tests cover
the model-facing contract: truthful resolution/provenance, bounded and
labeled evidence, workspace jailing, effect classification, and graceful
degradation for languages without a rich adapter.
"""

from __future__ import annotations

from pathlib import Path

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
        for d in ToolCatalog().build_tool_defs(mode="single", read_only=False)
    }
    assert "inspect_code" in names
    removed = {
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
        "code_intel_audit",
        "read_file_outline",
        "search_codebase",
    }
    assert not (names & removed)


def test_inspect_code_is_classified_as_observation() -> None:
    registry = ToolRegistry(workspace_root=Path("."), mode="single")
    assert registry.tool_effect("inspect_code") is ToolEffect.OBSERVATION


# ── 4: workspace jail ────────────────────────────────────────────────────


def test_inspect_code_rejects_paths_that_escape_the_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    payload = _inspect(registry, path="../outside.py")
    assert payload["ok"] is False
    assert "error" in payload


# ── 5/6: resolvable Python target returns bounded, truthfully-labeled evidence


def test_python_symbol_resolution_returns_bounded_source_and_truthful_target(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
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
    # Python's ast exposes an honest end range for this function, so the
    # blanket "we never know a body end" limitation must not be implied here.
    assert target["declaration_end_line"] == 9
    assert "exact_body_range_unavailable" not in payload["limitations"]
    assert "resolved_references_unavailable" in payload["limitations"]
    assert "call_graph_unavailable" in payload["limitations"]
    assert "type_resolution_unavailable" in payload["limitations"]

    excerpt = payload["source_excerpt"]
    assert excerpt["path"] == "app.py"
    assert "def target_fn" in excerpt["text"]
    assert excerpt["provenance"] == "filesystem"
    assert excerpt["parser_bounded"] is True
    assert excerpt["bounded_window"] is False


def test_line_anchor_without_symbol_resolves_by_line(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(tmp_path / "app.py", "def foo():\n    pass\n")

    payload = _inspect(registry, path="app.py", line=1)

    assert payload["target"]["resolution"] == "exact_line_match"
    assert payload["target"]["name"] == "foo"


def test_line_between_declarations_is_nearest_preceding_not_exact(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(tmp_path / "app.py", "def foo():\n    x = 1\n    return x\n")

    # Line 2 is inside foo's body, not a declaration line itself.
    payload = _inspect(registry, path="app.py", line=2)

    assert payload["target"]["resolution"] == "nearest_preceding_declaration"
    assert payload["target"]["name"] == "foo"


def test_unresolvable_anchor_still_returns_source(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(tmp_path / "app.py", "x = 1\ny = 2\n")

    payload = _inspect(registry, path="app.py", symbol="does_not_exist")

    assert payload["ok"] is True
    assert payload["target"]["resolution"] == "unresolved"
    assert payload["target"]["name"] is None
    assert payload["target"]["provenance"] is None
    assert payload["source_excerpt"]["text"] != ""


# ── 7/8: lexical occurrences are labeled and bounded ────────────────────


def test_occurrences_are_labeled_lexical_and_bounded(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(
        tmp_path / "app.py",
        "def shared_name():\n    pass\n",
    )
    _write(
        tmp_path / "other.py",
        "".join(f"shared_name()  # call {i}\n" for i in range(30)),
    )

    payload = _inspect(registry, path="app.py", symbol="shared_name")

    occurrences = payload["occurrences"]
    assert occurrences["provenance"] == "lexical_search"
    assert occurrences["returned"] <= 20
    assert occurrences["total"] >= occurrences["returned"]
    if occurrences["total"] > occurrences["returned"]:
        assert occurrences["truncated"] is True
    for item in occurrences["items"]:
        assert set(item) == {"path", "line", "context"}
    assert "occurrences_are_lexical" in payload["limitations"]


def test_no_symbol_no_occurrences(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    # A bare comment produces no top-level symbol, so line 1 resolves to
    # nothing and there is no symbol name to search occurrences for.
    _write(tmp_path / "app.py", "# just a comment\n")

    payload = _inspect(registry, path="app.py", line=1)

    assert payload["occurrences"] is None
    assert "occurrences_are_lexical" not in payload["limitations"]


# ── 9: graceful degradation for a language without a rich adapter ──────


def test_unsupported_language_still_returns_source_and_states_limitations(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(tmp_path / "notes.txt", "line one\nline two\nline three\n")

    payload = _inspect(registry, path="notes.txt", line=2)

    assert payload["ok"] is True
    assert payload["source_excerpt"]["text"] != ""
    assert payload["target"]["language"] in ("text", "unknown")
    assert "exact_body_range_unavailable" in payload["limitations"]


# ── 11/12/13/14: parser-owned declaration ranges ─────────────────────────


def test_resolved_python_target_reports_exact_parser_owned_range(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(
        tmp_path / "app.py",
        "def target_fn(x):\n    y = x + 1\n    return y\n",
    )

    payload = _inspect(registry, path="app.py", symbol="target_fn")

    target = payload["target"]
    assert target["declaration_line"] == 1
    assert target["declaration_end_line"] == 3
    assert target["declaration_end_column"] == len("    return y")
    # An exact range is now known, so the blanket limitation must not appear.
    assert "exact_body_range_unavailable" not in payload["limitations"]


def test_inspect_code_uses_parser_bounded_source_when_exact_range_known(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
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
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
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
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    _write(tmp_path / "notes.txt", "line one\nline two\nline three\n")

    payload = _inspect(registry, path="notes.txt", line=1)

    target = payload["target"]
    assert target["declaration_end_line"] is None
    assert target["declaration_end_column"] is None

    excerpt = payload["source_excerpt"]
    assert excerpt["parser_bounded"] is False
    assert excerpt["bounded_window"] is True
    assert "exact_body_range_unavailable" in payload["limitations"]


# ── 10: workspace-root change re-binds the inspector ────────────────────


def test_workspace_root_change_rebinds_inspection(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a / "app.py", "def only_in_a():\n    pass\n")
    _write(root_b / "app.py", "def only_in_b():\n    pass\n")

    registry = ToolRegistry(workspace_root=root_a, mode="single")
    first = _inspect(registry, path="app.py", line=1)
    assert first["target"]["name"] == "only_in_a"

    registry.set_workspace_root(root_b)
    second = _inspect(registry, path="app.py", line=1)
    assert second["target"]["name"] == "only_in_b"
