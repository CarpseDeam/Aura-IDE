"""Contract coverage for the two production evidence tools' schemas.

``search_codebase`` and ``inspect_code`` are the model's only two ranked/dense
evidence tools in the production catalog. This file proves their schema
descriptions communicate durable capability facts truthfully — ranked
*structural* retrieval rather than whole-file recall for the former, and a
source excerpt that can be genuinely declaration-bounded (not always a fixed
window) for the latter — by cross-checking the description text against what
the underlying implementations actually return. It does not pin exact prose.
"""

from __future__ import annotations

from pathlib import Path

import aura.code_intel  # noqa: F401 — triggers adapter registration
from aura.code_intel.index import CodeIntelIndex
from aura.codebase_index.indexer import CodebaseIndex
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.schemas.code_intel import INSPECT_CODE_TOOL_DEF
from aura.conversation.tools.schemas.search import SEARCH_TOOL_DEFS

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _search_codebase_def() -> dict:
    for tool in SEARCH_TOOL_DEFS:
        if tool["function"]["name"] == "search_codebase":
            return tool
    raise AssertionError("search_codebase not found in SEARCH_TOOL_DEFS")


# ── search_codebase: description matches structural result shape ───────────


def test_search_codebase_result_fields_match_the_schema_description(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "auth.py",
        "class AuthHandler:\n    def authenticate(self, user):\n        return True\n",
    )
    index = CodebaseIndex(tmp_path, code_intel_index=CodeIntelIndex(tmp_path))
    result = index.search("authenticate")

    assert result["ok"] is True
    assert result["results"], "expected at least one structural retrieval hit"
    hit = result["results"][0]
    # The truthful structural fields the implementation actually returns.
    for field in ("path", "score", "snippet", "start_line", "end_line", "symbol", "symbol_kind", "parent"):
        assert field in hit, field

    description = _search_codebase_def()["function"]["description"].lower()
    # The schema names the structural metadata fields the model will see.
    for field_mention in ("start_line", "end_line", "symbol_kind", "parent"):
        assert field_mention in description, field_mention


def test_search_codebase_description_no_longer_claims_whole_file_ranking() -> None:
    description = _search_codebase_def()["function"]["description"].lower()
    assert "rank whole files" not in description
    assert "bounded" in description
    assert "structure" in description


def test_search_codebase_description_has_no_workflow_coaching() -> None:
    description = _search_codebase_def()["function"]["description"].lower()
    for phrase in ("use this only when", "use grep_search", "always call this first"):
        assert phrase not in description, phrase


# ── inspect_code: description matches the declaration-bounded excerpt ──────


def test_inspect_code_description_reflects_parser_bounded_excerpt(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    _write(tmp_path / "app.py", "def target_fn(x):\n    y = x + 1\n    return y\n")

    result = registry.execute(
        "inspect_code", {"path": "app.py", "symbol": "target_fn"}, approval_cb=_APPROVE
    )
    excerpt = result.payload["source_excerpt"]
    assert excerpt["parser_bounded"] is True

    description = INSPECT_CODE_TOOL_DEF["function"]["description"].lower()
    assert "parser_bounded" in description
    assert "parser-owned declaration" in description


def test_inspect_code_description_does_not_claim_ranges_always_unavailable() -> None:
    description = INSPECT_CODE_TOOL_DEF["function"]["description"].lower()
    # The old description bundled "exact declaration ranges" into the same
    # blanket unavailable list as resolved references/call graphs/type
    # inference. It is no longer always true, so that exact conjunction must
    # not appear.
    assert "type inference, exact declaration ranges" not in description
    # The corrected wording states the condition under which a range is absent.
    assert "did not parse one" in description
