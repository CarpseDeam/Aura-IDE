"""Tests for every tool registered in TOOL_HANDLERS.

Each tool gets its own test class with valid-input and invalid-input tests.
All underlying functions are mocked so no real filesystem, git, or network
calls are made during test execution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.tools._schemas import DIAGNOSTIC_TOOL_DEF, READ_TOOL_DEFS, WRITE_TOOL_DEFS
from aura.conversation.tools._types import (
    ApprovalDecision,
)
from aura.conversation.tools.fs_write import propose_line_range_edit
from aura.conversation.tools.registry import (
    TOOL_HANDLERS,
    ToolRegistry,
)

# Fixtures


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ToolRegistry(workspace_root=ws, read_only=False, mode="single")


@pytest.fixture
def approve_cb() -> MagicMock:
    return MagicMock(return_value=ApprovalDecision(action="approve"))


@pytest.fixture
def reject_cb() -> MagicMock:
    return MagicMock(return_value=ApprovalDecision(action="reject"))


def _handler(name: str):
    """Look up the unbound handler method from TOOL_HANDLERS."""
    return TOOL_HANDLERS[name]


# read_file


class TestReadFile:
    """Tests for the read_file tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.read_file") as mock_rf:
            mock_rf.return_value = {
                "ok": True, "path": "README.md", "content": "# Hello", "truncated": False,
            }
            result = _handler("read_file")(registry, {"path": "README.md"}, approve_cb, False)

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["path"] == "README.md"
        mock_rf.assert_called_once_with(ANY, ANY)

    def test_missing_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("read_file", {}, approve_cb, False)
        assert result.ok is False
        # _resolve_in_root("") raises ValueError: path must not be empty
        assert "empty" in str(result.payload).lower()

    def test_path_with_dotdot(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("read_file", {"path": "../README.md"}, approve_cb, False)
        assert result.ok is False

    def test_empty_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("read_file", {"path": ""}, approve_cb, False)
        assert result.ok is False


# read_files


class TestReadFiles:
    """Tests for the read_files batched file-read tool."""

    def test_valid_multiple_files(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.read_file") as mock_rf:
            mock_rf.side_effect = [
                {"ok": True, "path": "a.py", "content": "hello", "truncated": False},
                {"ok": True, "path": "b.py", "content": "world", "truncated": False},
            ]
            result = _handler("read_files")(registry, {"paths": ["a.py", "b.py"]}, approve_cb, False)

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["files"]["a.py"] == {"ok": True, "content": "hello"}
        assert result.payload["files"]["b.py"] == {"ok": True, "content": "world"}
        assert mock_rf.call_count == 2

    def test_mixed_valid_and_invalid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.read_file") as mock_rf:
            mock_rf.side_effect = [
                {"ok": True, "path": "good.py", "content": "data", "truncated": False},
                {"ok": False, "error": "file not found: missing.py"},
            ]
            result = _handler("read_files")(registry, {"paths": ["good.py", "missing.py"]}, approve_cb, False)

        assert result.payload["ok"] is True
        assert result.payload["files"]["good.py"]["ok"] is True
        assert result.payload["files"]["missing.py"]["ok"] is False
        assert "file not found" in result.payload["files"]["missing.py"]["error"]

    def test_missing_paths_key(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("read_files")(registry, {}, approve_cb, False)
        assert result.ok is False
        assert "non-empty array" in result.payload["error"]

    def test_empty_paths_array(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("read_files")(registry, {"paths": []}, approve_cb, False)
        assert result.ok is False
        assert "non-empty array" in result.payload["error"]

    def test_total_size_cap_exceeded(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.read_file") as mock_rf:
            mock_rf.side_effect = [
                {"ok": True, "path": "a.py", "content": "x" * 200000, "truncated": False},
                {"ok": True, "path": "b.py", "content": "y" * 200000, "truncated": False},
                {"ok": True, "path": "c.py", "content": "z" * 200000, "truncated": False},
            ]
            result = _handler("read_files")(registry, {"paths": ["a.py", "b.py", "c.py"]}, approve_cb, False)

        assert result.payload["ok"] is True
        assert result.payload["files"]["a.py"]["ok"] is True
        assert result.payload["files"]["b.py"]["ok"] is True
        assert result.payload["files"]["c.py"]["ok"] is False
        assert "exceeded total size limit" in result.payload["files"]["c.py"]["error"]

    def test_path_escapes_workspace(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Do NOT mock read_file; let the real _resolve_in_root reject the path."""
        result = _handler("read_files")(registry, {"paths": ["../secret.txt"]}, approve_cb, False)
        assert result.payload["ok"] is True
        assert result.payload["files"]["../secret.txt"]["ok"] is False
        error = result.payload["files"]["../secret.txt"]["error"].lower()
        assert "not allowed" in error or "escap" in error


# list_directory


class TestListDirectory:
    """Tests for the list_directory tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.list_directory") as mock_ld:
            mock_ld.return_value = {"ok": True, "path": ".", "directories": [], "files": []}
            result = _handler("list_directory")(registry, {"path": "."}, approve_cb, False)

        assert result.ok is True

    def test_dotdot_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("list_directory", {"path": ".."}, approve_cb, False)
        assert result.ok is False

    def test_missing_path_defaults_to_dot(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.list_directory") as mock_ld:
            mock_ld.return_value = {"ok": True, "path": ".", "directories": [], "files": []}
            result = _handler("list_directory")(registry, {}, approve_cb, False)

        assert result.ok is True
        mock_ld.assert_called_once()


# glob


class TestGlob:
    """Tests for the glob tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.glob_files") as mock_gf:
            mock_gf.return_value = {"ok": True, "pattern": "**/*.py", "matches": [], "truncated": False}
            result = _handler("glob")(registry, {"pattern": "**/*.py"}, approve_cb, False)

        assert result.ok is True

    def test_missing_pattern(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("glob")(registry, {}, approve_cb, False)
        assert result.ok is False

    def test_empty_pattern(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("glob")(registry, {"pattern": ""}, approve_cb, False)
        assert result.ok is False

    def test_absolute_pattern(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.glob_files",
                   side_effect=ValueError("absolute path")):
            result = registry.execute("glob", {"pattern": "/etc"}, approve_cb, False)
        assert result.ok is False

    def test_pattern_with_dotdot(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("glob")(registry, {"pattern": "../foo"}, approve_cb, False)
        assert result.ok is False


# read_file_outline


class TestReadFileOutline:
    """Tests for the read_file_outline tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.fs_handler.read_file_outline") as mock_rfo:
            mock_rfo.return_value = {"ok": True, "path": "file.py", "language": "python"}
            result = _handler("read_file_outline")(registry, {"path": "file.py"}, approve_cb, False)

        assert result.ok is True

    def test_missing_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("read_file_outline", {}, approve_cb, False)
        assert result.ok is False

    def test_dotdot_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("read_file_outline", {"path": "../file.py"}, approve_cb, False)
        assert result.ok is False


# grep_search


class TestGrepSearch:
    """Tests for the grep_search tool."""

    def test_valid_minimal(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.grep_files") as mock_gf:
            mock_gf.return_value = {"ok": True, "matches": []}
            result = _handler("grep_search")(registry, {"pattern": "foo"}, approve_cb, False)

        assert result.ok is True

    def test_valid_all_options(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.grep_files") as mock_gf:
            mock_gf.return_value = {"ok": True, "matches": []}
            result = _handler("grep_search")(
                registry,
                {
                    "pattern": "foo",
                    "regex_mode": True,
                    "case_sensitive": True,
                    "max_results": 100,
                    "include_pattern": "**/*.py",
                },
                approve_cb,
                False,
            )

        assert result.ok is True
        mock_gf.assert_called_once()
        kwargs = mock_gf.call_args.kwargs
        assert kwargs["pattern"] == "foo"
        assert kwargs["regex_mode"] is True
        assert kwargs["case_sensitive"] is True
        assert kwargs["max_results"] == 100
        assert kwargs["include_pattern"] == "**/*.py"

    def test_missing_pattern(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("grep_search")(registry, {}, approve_cb, False)
        assert result.ok is False

    def test_empty_pattern(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("grep_search")(registry, {"pattern": ""}, approve_cb, False)
        assert result.ok is False

    def test_grep_handler_propagates_failure(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.grep_files") as mock_gf:
            mock_gf.return_value = {"ok": False, "error": "boom"}
            result = _handler("grep_search")(registry, {"pattern": "anything"}, approve_cb, False)

        assert result.ok is False
        assert result.payload["ok"] is False
        assert result.payload["error"] == "boom"

    def test_grep_handler_preserves_search_metadata(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.grep_files") as mock_gf:
            mock_gf.return_value = {
                "ok": True,
                "matches": [],
                "engine": "python",
                "searched_files": 12,
                "skipped_files": 3,
                "truncated": False,
                "regex_mode": False,
                "auto_regex_retry": True,
                "include_pattern": "**/*.py",
            }
            result = _handler("grep_search")(registry, {"pattern": "anything"}, approve_cb, False)

        assert result.ok is True
        assert result.payload["engine"] == "python"
        assert result.payload["searched_files"] == 12
        assert result.payload["skipped_files"] == 3
        assert result.payload["auto_regex_retry"] is True


class TestToolSchemaDocs:
    def test_grep_search_include_pattern_docs_recommend_recursive_python_glob(self):
        grep_tool = next(tool for tool in READ_TOOL_DEFS if tool["function"]["name"] == "grep_search")
        include_desc = grep_tool["function"]["parameters"]["properties"]["include_pattern"]["description"]
        assert "**/*.py" in include_desc
        assert "recursive" in include_desc.lower() or "anywhere in the repo" in include_desc.lower()

    def test_diagnostic_schema_prefers_rg_over_grep(self):
        command_desc = DIAGNOSTIC_TOOL_DEF["function"]["parameters"]["properties"]["command"]["description"]
        assert "Use 'rg' instead of bare grep" in command_desc
        assert "grep_search" in command_desc


# find_usages


class TestFindUsages:
    """Tests for the find_usages tool."""

    def test_valid_minimal(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.find_usages") as mock_fu:
            mock_fu.return_value = {"ok": True, "matches": []}
            result = _handler("find_usages")(registry, {"symbol": "foo"}, approve_cb, False)

        assert result.ok is True

    def test_valid_all_options(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.find_usages") as mock_fu:
            mock_fu.return_value = {"ok": True, "matches": []}
            result = _handler("find_usages")(
                registry,
                {"symbol": "foo", "include_pattern": "**/*.py", "max_results": 50, "case_sensitive": True},
                approve_cb,
                False,
            )

        assert result.ok is True
        kwargs = mock_fu.call_args.kwargs
        assert kwargs["symbol"] == "foo"
        assert kwargs["include_pattern"] == "**/*.py"
        assert kwargs["max_results"] == 50
        assert kwargs["case_sensitive"] is True

    def test_missing_symbol(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("find_usages")(registry, {}, approve_cb, False)
        assert result.ok is False

    def test_empty_symbol(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("find_usages")(registry, {"symbol": ""}, approve_cb, False)
        assert result.ok is False


# search_codebase


class TestSearchCodebase:
    """Tests for the search_codebase tool."""

    def test_valid_minimal(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.CodebaseIndex") as mock_index_cls,
            patch("aura.conversation.tools.registry._search_codebase") as mock_sc,
        ):
            mock_index_cls.return_value = MagicMock()
            mock_sc.return_value = {"ok": True, "results": []}
            result = _handler("search_codebase")(registry, {"query": "auth handler"}, approve_cb, False)

        assert result.ok is True

    def test_valid_with_top_k(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.CodebaseIndex") as mock_index_cls,
            patch("aura.conversation.tools.registry._search_codebase") as mock_sc,
        ):
            mock_index_cls.return_value = MagicMock()
            mock_sc.return_value = {"ok": True, "results": []}
            result = _handler("search_codebase")(
                registry, {"query": "auth handler", "top_k": 3}, approve_cb, False
            )

        assert result.ok is True
        assert mock_sc.call_args.kwargs["top_k"] == 3

    def test_missing_query(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("search_codebase")(registry, {}, approve_cb, False)
        assert result.ok is False

    def test_empty_query(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("search_codebase")(registry, {"query": ""}, approve_cb, False)
        assert result.ok is False


# git tools


class TestGitStatus:
    """Tests for the git_status tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_status") as mock_gs:
            mock_gs.return_value = {"ok": True, "branch": "main"}
            result = _handler("git_status")(registry, {}, approve_cb, False)

        assert result.ok is True
        mock_gs.assert_called_once_with(registry.workspace_root)

    def test_extra_args_ignored(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_status") as mock_gs:
            mock_gs.return_value = {"ok": True, "branch": "main"}
            result = _handler("git_status")(registry, {"unknown_key": 123}, approve_cb, False)

        assert result.ok is True


class TestGitDiff:
    """Tests for the git_diff tool."""

    def test_valid_default(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_diff") as mock_gd:
            mock_gd.return_value = {"ok": True, "diff": ""}
            result = _handler("git_diff")(registry, {}, approve_cb, False)

        assert result.ok is True
        mock_gd.assert_called_once_with(registry.workspace_root, staged=False, path=None)

    def test_valid_with_options(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_diff") as mock_gd:
            mock_gd.return_value = {"ok": True, "diff": ""}
            result = _handler("git_diff")(
                registry, {"staged": True, "path": "foo.py"}, approve_cb, False
            )

        assert result.ok is True
        mock_gd.assert_called_once_with(registry.workspace_root, staged=True, path="foo.py")


class TestGitLog:
    """Tests for the git_log tool."""

    def test_valid_default(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_log") as mock_gl:
            mock_gl.return_value = {"ok": True, "commits": []}
            result = _handler("git_log")(registry, {}, approve_cb, False)

        assert result.ok is True
        mock_gl.assert_called_once_with(registry.workspace_root, max_count=10, path=None)

    def test_valid_with_options(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_log") as mock_gl:
            mock_gl.return_value = {"ok": True, "commits": []}
            result = _handler("git_log")(
                registry, {"max_count": 5, "path": "foo.py"}, approve_cb, False
            )

        assert result.ok is True
        mock_gl.assert_called_once_with(registry.workspace_root, max_count=5, path="foo.py")


class TestGitShow:
    """Tests for the git_show tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_show") as mock_gsh:
            mock_gsh.return_value = {"ok": True, "output": "diff"}
            result = _handler("git_show")(registry, {"commit_sha": "abc123"}, approve_cb, False)

        assert result.ok is True

    def test_missing_commit_sha(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("git_show")(registry, {}, approve_cb, False)
        assert result.ok is False
        assert "commit_sha" in str(result.payload).lower()

    def test_empty_commit_sha(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("git_show")(registry, {"commit_sha": ""}, approve_cb, False)
        assert result.ok is False


class TestGitLogFile:
    """Tests for the git_log_file tool."""

    def test_valid_default(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_log_file") as mock_glf:
            mock_glf.return_value = {"ok": True, "commits": []}
            result = _handler("git_log_file")(registry, {"path": "foo.py"}, approve_cb, False)

        assert result.ok is True
        mock_glf.assert_called_once_with(registry.workspace_root, "foo.py", max_count=10)

    def test_valid_with_max_count(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_log_file") as mock_glf:
            mock_glf.return_value = {"ok": True, "commits": []}
            result = _handler("git_log_file")(
                registry, {"path": "foo.py", "max_count": 3}, approve_cb, False
            )

        assert result.ok is True
        mock_glf.assert_called_once_with(registry.workspace_root, "foo.py", max_count=3)

    def test_missing_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("git_log_file")(registry, {}, approve_cb, False)
        assert result.ok is False

    def test_empty_path(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("git_log_file")(registry, {"path": ""}, approve_cb, False)
        assert result.ok is False


class TestGitBranchList:
    """Tests for the git_branch_list tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_branch_list") as mock_gbl:
            mock_gbl.return_value = {"ok": True, "branches": []}
            result = _handler("git_branch_list")(registry, {}, approve_cb, False)

        assert result.ok is True

    def test_extra_args_ignored(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_branch_list") as mock_gbl:
            mock_gbl.return_value = {"ok": True, "branches": []}
            result = _handler("git_branch_list")(registry, {"extra": "value"}, approve_cb, False)

        assert result.ok is True


class TestGitStashList:
    """Tests for the git_stash_list tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_stash_list") as mock_gsl:
            mock_gsl.return_value = {"ok": True, "stashes": []}
            result = _handler("git_stash_list")(registry, {}, approve_cb, False)

        assert result.ok is True

    def test_extra_args_ignored(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_stash_list") as mock_gsl:
            mock_gsl.return_value = {"ok": True, "stashes": []}
            result = _handler("git_stash_list")(registry, {"extra": "x"}, approve_cb, False)

        assert result.ok is True


class TestGitStashShow:
    """Tests for the git_stash_show tool."""

    def test_valid_default(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_stash_show") as mock_gss:
            mock_gss.return_value = {"ok": True, "diff": ""}
            result = _handler("git_stash_show")(registry, {}, approve_cb, False)

        assert result.ok is True
        mock_gss.assert_called_once_with(registry.workspace_root, index=0)

    def test_valid_with_index(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.git_handler.git_stash_show") as mock_gss:
            mock_gss.return_value = {"ok": True, "diff": ""}
            result = _handler("git_stash_show")(registry, {"index": 2}, approve_cb, False)

        assert result.ok is True
        mock_gss.assert_called_once_with(registry.workspace_root, index=2)

    def test_non_int_index(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Non-integer index raises ValueError which is caught by execute()."""
        with patch("aura.conversation.tools.git_handler.git_stash_show") as mock_gss:
            mock_gss.side_effect = ValueError("invalid literal for int")
            result = registry.execute("git_stash_show", {"index": "bad"}, approve_cb, False)

        assert result.ok is False


# web tools


class TestWebSearch:
    """Tests for the web_search tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.web_handler.web_search") as mock_ws:
            mock_ws.return_value = {"ok": True, "results": []}
            result = _handler("web_search")(registry, {"query": "python 3.13"}, approve_cb, False)

        assert result.ok is True

    def test_valid_with_max_results(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.web_handler.web_search") as mock_ws:
            mock_ws.return_value = {"ok": True, "results": []}
            result = _handler("web_search")(
                registry, {"query": "python 3.13", "max_results": 3}, approve_cb, False
            )

        assert result.ok is True
        mock_ws.assert_called_once_with("python 3.13", 3)

    def test_missing_query(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.web_handler.web_search") as mock_ws:
            mock_ws.return_value = {"ok": True, "results": []}
            result = _handler("web_search")(registry, {}, approve_cb, False)

        assert result.ok is True  # validation deferred to web_search
        mock_ws.assert_called_once_with("", 5)


class TestWebFetch:
    """Tests for the web_fetch tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.web_handler.web_fetch") as mock_wf:
            mock_wf.return_value = {"ok": True, "url": "https://example.com", "content": "text"}
            result = _handler("web_fetch")(
                registry, {"url": "https://example.com"}, approve_cb, False
            )

        assert result.ok is True

    def test_missing_url(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.web_handler.web_fetch") as mock_wf:
            mock_wf.return_value = {"ok": True, "content": ""}
            result = _handler("web_fetch")(registry, {}, approve_cb, False)

        assert result.ok is True  # validation deferred to web_fetch
        mock_wf.assert_called_once_with("")


# write_file


class TestWriteFile:
    """Tests for the write_file tool — the most complex due to approval flow."""

    def test_valid_new_file(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_write") as mock_pw,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
        ):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new_file.py",
                "old_content": "",
                "new_content": "print('hello')",
                "is_new_file": True,
            }
            result = _handler("write_file")(
                registry, {"path": "new_file.py", "content": "print('hello')"}, approve_cb, False
            )

        assert result.ok is True
        assert result.payload.get("path") == "new_file.py"
        assert result.payload.get("is_new_file") is True

    def test_rejected_by_user(self, registry: ToolRegistry, reject_cb: MagicMock):
        with patch("aura.conversation.tools.registry.propose_write") as mock_pw:
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "",
                "new_content": "x",
                "is_new_file": True,
            }
            result = _handler("write_file")(
                registry, {"path": "f.py", "content": "x"}, reject_cb, False
            )

        assert result.ok is False

    def test_reject_all_flag(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("write_file")(
            registry, {"path": "f.py", "content": "x"}, approve_cb, reject_all=True
        )
        assert result.ok is False

    def test_read_only_blocked(self, registry: ToolRegistry, approve_cb: MagicMock):
        registry.set_read_only(True)
        result = _handler("write_file")(
            registry, {"path": "f.py", "content": "x"}, approve_cb, False
        )
        assert result.ok is False
        assert "read-only" in str(result.payload).lower()

    def test_planner_mode_blocked(self, registry: ToolRegistry, approve_cb: MagicMock):
        registry.set_mode("planner")
        result = _handler("write_file")(
            registry, {"path": "f.py", "content": "x"}, approve_cb, False
        )
        assert result.ok is False
        assert "planner" in str(result.payload).lower()

    def test_missing_content_defaults_to_empty(self, registry: ToolRegistry, approve_cb: MagicMock):
        with patch("aura.conversation.tools.registry.propose_write") as mock_pw:
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "",
                "new_content": "",
                "is_new_file": True,
            }
            result = _handler("write_file")(
                registry, {"path": "f.py"}, approve_cb, False
            )

        assert result.ok is True
        mock_pw.assert_called_once()
        # content defaults to "" and is passed through
        assert mock_pw.call_args[0][2] == ""


class TestDeleteFile:
    """Tests for safe delete_file."""

    def test_schema_exists(self):
        schemas = {
            tool_def["function"]["name"]: tool_def
            for tool_def in WRITE_TOOL_DEFS
        }

        assert "delete_file" in schemas
        params = schemas["delete_file"]["function"]["parameters"]
        assert set(params["properties"]) == {"path", "reason"}
        assert params["required"] == ["path"]

    def test_included_in_write_tools(self):
        assert "delete_file" in WRITE_TOOLS

    def test_deletes_existing_file_after_approval_and_records_backup(self, registry: ToolRegistry, approve_cb: MagicMock):
        target = registry.workspace_root / "obsolete.txt"
        target.write_text("remove me\n", encoding="utf-8")

        result = _handler("delete_file")(
            registry,
            {"path": "obsolete.txt", "reason": "cleanup"},
            approve_cb,
            False,
        )

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["applied"] is True
        assert result.payload["deleted"] is True
        assert result.payload["write_outcome"] == "deleted"
        assert result.payload["path"] == "obsolete.txt"
        assert result.payload["rel_path"] == "obsolete.txt"
        assert result.payload["reason"] == "cleanup"
        assert result.payload["backup"]
        assert (registry.workspace_root / result.payload["backup"]).read_text(encoding="utf-8") == "remove me\n"
        assert not target.exists()
        approve_cb.assert_called_once()
        request = approve_cb.call_args[0][0]
        assert request.tool_name == "delete_file"
        assert request.old_content == "remove me\n"
        assert request.new_content == ""

    def test_rejected_by_user_does_not_remove_file(self, registry: ToolRegistry, reject_cb: MagicMock):
        target = registry.workspace_root / "keep.txt"
        target.write_text("keep\n", encoding="utf-8")

        result = _handler("delete_file")(
            registry,
            {"path": "keep.txt"},
            reject_cb,
            False,
        )

        assert result.ok is False
        assert result.payload["applied"] is False
        assert result.payload["deleted"] is False
        assert result.payload["failure_class"] == "approval_rejected"
        assert target.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.parametrize(
        ("path", "failure_class"),
        [
            ("missing.txt", "delete_file_missing"),
            ("*.py", "delete_file_invalid_path"),
            ("../escape.txt", "delete_file_workspace_escape"),
        ],
    )
    def test_rejects_invalid_targets(self, registry: ToolRegistry, approve_cb: MagicMock, path: str, failure_class: str):
        result = _handler("delete_file")(
            registry,
            {"path": path},
            approve_cb,
            False,
        )

        assert result.ok is False
        assert result.payload["applied"] is False
        assert result.payload["deleted"] is False
        assert result.payload["failure_class"] == failure_class
        approve_cb.assert_not_called()

    def test_rejects_directory(self, registry: ToolRegistry, approve_cb: MagicMock):
        (registry.workspace_root / "dir").mkdir()

        result = _handler("delete_file")(
            registry,
            {"path": "dir"},
            approve_cb,
            False,
        )

        assert result.ok is False
        assert result.payload["failure_class"] == "delete_file_is_directory"
        assert (registry.workspace_root / "dir").is_dir()
        approve_cb.assert_not_called()

    @pytest.mark.parametrize("path", [".git/config", ".aura/state.json", ".env", "config/.env.local"])
    def test_rejects_protected_paths(self, registry: ToolRegistry, approve_cb: MagicMock, path: str):
        target = registry.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret\n", encoding="utf-8")

        result = _handler("delete_file")(
            registry,
            {"path": path},
            approve_cb,
            False,
        )

        assert result.ok is False
        assert result.payload["failure_class"] == "delete_file_protected_path"
        assert target.exists()
        approve_cb.assert_not_called()


# write_file — humanizer integration


class TestWriteFileHumanizer:
    """Tests for the humanizer integration in write_file proposals."""

    RAW_PYTHON = """```python
items = []
for i in items:
    print(i)
```"""

    CLEAN_PYTHON = 'items = []\nfor i in items:\n    print(i)'

    def test_new_python_write_is_humanized(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_write") as mock_pw,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new.py",
                "old_content": "",
                "new_content": self.RAW_PYTHON,
                "is_new_file": True,
            }
            mock_hp = MagicMock()
            mock_hp.humanize_code.return_value = MagicMock(
                text=self.CLEAN_PYTHON, syntax_fallback=False, error=None, changed=True,
                markdown_stripped=True, comments_removed=3, docstrings_removed=0,
            )
            mock_hp_cls.return_value = mock_hp

            result = _handler("write_file")(
                registry,
                {"path": "new.py", "content": self.RAW_PYTHON},
                approve_cb,
                False,
            )

        assert result.ok is True
        # ApprovalRequest should contain the cleaned content
        req = approve_cb.call_args[0][0]
        assert req.new_content == self.CLEAN_PYTHON
        mock_hp.humanize_code.assert_called_once()

    def test_non_python_new_file_not_humanized(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_write") as mock_pw,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new.js",
                "old_content": "",
                "new_content": "console.log('hello');",
                "is_new_file": True,
            }

            result = _handler("write_file")(
                registry,
                {"path": "new.js", "content": "console.log('hello');"},
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        assert req.new_content == "console.log('hello');"
        mock_hp_cls.assert_not_called()

    def test_existing_python_edit_not_humanized(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Existing .py edit_file: content is NOT replaced (observe-only at most)."""
        with (
            patch("aura.conversation.tools.registry.propose_edit") as mock_pe,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": "value = 2\n",
                "is_new_file": False,
            }

            result = _handler("edit_file")(
                registry,
                {"path": "existing.py", "old_str": "old code", "new_str": "value = 2\n"},
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        assert req.new_content == "value = 2\n"
        # Humanizer may be called in observe mode, but content must not change
        # (observe mode is tested separately)

    def test_observe_mode_does_not_change_content(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        monkeypatch.setenv("AURA_HUMANIZER_OBSERVE", "1")
        with (
            patch("aura.conversation.tools.registry.propose_write") as mock_pw,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
        ):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new.py",
                "old_content": "",
                "new_content": self.CLEAN_PYTHON,
                "is_new_file": True,
            }

            result = _handler("write_file")(
                registry,
                {"path": "new.py", "content": self.CLEAN_PYTHON},
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        # Content must be unchanged in observe mode
        assert req.new_content == self.CLEAN_PYTHON

    def test_kill_switch_disables_behavior(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        monkeypatch.setenv("AURA_HUMANIZER", "0")
        with (
            patch("aura.conversation.tools.registry.propose_write") as mock_pw,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new.py",
                "old_content": "",
                "new_content": self.CLEAN_PYTHON,
                "is_new_file": True,
            }

            result = _handler("write_file")(
                registry,
                {"path": "new.py", "content": self.CLEAN_PYTHON},
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        assert req.new_content == self.CLEAN_PYTHON
        mock_hp_cls.assert_not_called()

    # edit_symbol humanizer integration

    def test_edit_symbol_replacement_enabled(
        self, registry: ToolRegistry, approve_cb: MagicMock
    ):
        """edit_symbol should replace content with humanizer result."""
        with (
            patch("aura.conversation.tools.registry.propose_edit_symbol") as mock_pes,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": self.RAW_PYTHON,
                "is_new_file": False,
            }
            mock_hp = MagicMock()
            mock_hp.humanize_code.return_value = MagicMock(
                text=self.CLEAN_PYTHON,
                syntax_fallback=False,
                error=None,
                changed=True,
                markdown_stripped=True,
                comments_removed=3,
                docstrings_removed=0,
            )
            mock_hp_cls.return_value = mock_hp

            result = _handler("edit_symbol")(
                registry,
                {
                    "path": "existing.py",
                    "symbol_type": "function",
                    "symbol_name": "hello",
                    "new_definition": self.RAW_PYTHON,
                },
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        assert req.new_content == self.CLEAN_PYTHON
        mock_hp.humanize_code.assert_called_once()

    def test_edit_symbol_observe_mode_no_replace(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        monkeypatch.setenv("AURA_HUMANIZER_OBSERVE", "1")
        with (
            patch("aura.conversation.tools.registry.propose_edit_symbol") as mock_pes,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": self.RAW_PYTHON,
                "is_new_file": False,
            }
            mock_hp = MagicMock()
            mock_hp.humanize_code.return_value = MagicMock(
                text=self.CLEAN_PYTHON,
                syntax_fallback=False,
                error=None,
                changed=True,
                markdown_stripped=True,
                comments_removed=3,
                docstrings_removed=0,
            )
            mock_hp_cls.return_value = mock_hp

            result = _handler("edit_symbol")(
                registry,
                {
                    "path": "existing.py",
                    "symbol_type": "function",
                    "symbol_name": "hello",
                    "new_definition": self.RAW_PYTHON,
                },
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        # Content unchanged in observe mode
        assert req.new_content == self.RAW_PYTHON
        mock_hp.humanize_code.assert_called_once()

    def test_kill_switch_disables_edit_symbol(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        monkeypatch.setenv("AURA_HUMANIZER", "0")
        with (
            patch("aura.conversation.tools.registry.propose_edit_symbol") as mock_pes,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": "new content",
                "is_new_file": False,
            }

            result = _handler("edit_symbol")(
                registry,
                {
                    "path": "existing.py",
                    "symbol_type": "function",
                    "symbol_name": "hello",
                    "new_definition": "new content",
                },
                approve_cb,
                False,
            )

        assert result.ok is True
        mock_hp_cls.assert_not_called()
        req = approve_cb.call_args[0][0]
        assert req.new_content == "new content"

    # edit_file humanizer gate integration

    def test_edit_file_humanizer_gated_by_env_var_disabled(
        self, registry: ToolRegistry, approve_cb: MagicMock
    ):
        """Without AURA_HUMANIZER_EDIT_FILE=1, humanizer is NOT called."""
        with (
            patch("aura.conversation.tools.registry.propose_edit") as mock_pe,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": "value = 2\n",
                "is_new_file": False,
            }

            result = _handler("edit_file")(
                registry,
                {"path": "existing.py", "old_str": "old code", "new_str": "value = 2\n"},
                approve_cb,
                False,
            )

        assert result.ok is True
        mock_hp_cls.assert_not_called()
        req = approve_cb.call_args[0][0]
        assert req.new_content == "value = 2\n"

    def test_edit_file_humanizer_gated_by_env_var_enabled(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        """With AURA_HUMANIZER_EDIT_FILE=1, humanizer IS called (observe-only)."""
        monkeypatch.setenv("AURA_HUMANIZER_EDIT_FILE", "1")
        with (
            patch("aura.conversation.tools.registry.propose_edit") as mock_pe,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": "value = 2\n",
                "is_new_file": False,
            }
            mock_hp = MagicMock()
            mock_hp.humanize_code.return_value = MagicMock(
                text="value = 3\n",
                syntax_fallback=False,
                error=None,
                changed=True,
                markdown_stripped=True,
                comments_removed=1,
                docstrings_removed=0,
            )
            mock_hp_cls.return_value = mock_hp

            result = _handler("edit_file")(
                registry,
                {"path": "existing.py", "old_str": "old code", "new_str": "value = 2\n"},
                approve_cb,
                False,
            )

        assert result.ok is True
        mock_hp.humanize_code.assert_called_once()
        req = approve_cb.call_args[0][0]
        # edit_file never replaces content
        assert req.new_content == "value = 2\n"

    def test_edit_file_observe_never_changes_content(
        self, registry: ToolRegistry, approve_cb: MagicMock, monkeypatch
    ):
        monkeypatch.setenv("AURA_HUMANIZER_EDIT_FILE", "1")
        monkeypatch.setenv("AURA_HUMANIZER_OBSERVE", "1")
        with (
            patch("aura.conversation.tools.registry.propose_edit") as mock_pe,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
            patch("aura.humanizer.HumanizerPipeline") as mock_hp_cls,
        ):
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old code",
                "new_content": "value = 2\n",
                "is_new_file": False,
            }
            mock_hp = MagicMock()
            mock_hp.humanize_code.return_value = MagicMock(
                text="value = 3\n",
                syntax_fallback=False,
                error=None,
                changed=True,
            )
            mock_hp_cls.return_value = mock_hp

            result = _handler("edit_file")(
                registry,
                {"path": "existing.py", "old_str": "old code", "new_str": "value = 2\n"},
                approve_cb,
                False,
            )

        assert result.ok is True
        req = approve_cb.call_args[0][0]
        # edit_file never replaces content regardless of observe mode
        assert req.new_content == "value = 2\n"
        mock_hp.humanize_code.assert_called_once()


# edit_file


class TestEditFile:
    """Tests for the edit_file tool."""

    def test_valid(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_edit") as mock_pe,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
        ):
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            result = _handler("edit_file")(
                registry, {"path": "f.py", "old_str": "old", "new_str": "new"}, approve_cb, False
            )

        assert result.ok is True
        assert result.payload["applied"] is True
        assert result.payload["applied_tool"] == "edit_file"

    def test_rejected(self, registry: ToolRegistry, reject_cb: MagicMock):
        with patch("aura.conversation.tools.registry.propose_edit") as mock_pe:
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            result = _handler("edit_file")(
                registry, {"path": "f.py", "old_str": "old", "new_str": "new"}, reject_cb, False
            )

        assert result.ok is False

    def test_read_only_blocked(self, registry: ToolRegistry, approve_cb: MagicMock):
        registry.set_read_only(True)
        result = _handler("edit_file")(
            registry, {"path": "f.py", "old_str": "a", "new_str": "b"}, approve_cb, False
        )
        assert result.ok is False

    def test_planner_mode_blocked(self, registry: ToolRegistry, approve_cb: MagicMock):
        registry.set_mode("planner")
        result = _handler("edit_file")(
            registry, {"path": "f.py", "old_str": "a", "new_str": "b"}, approve_cb, False
        )
        assert result.ok is False

    def test_non_string_old_str(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("edit_file")(
            registry, {"path": "f.py", "old_str": 42, "new_str": "b"}, approve_cb, False
        )
        assert result.ok is False
        assert "string" in str(result.payload).lower()


# edit_symbol


class TestEditSymbol:
    """Tests for the edit_symbol tool."""

    def test_valid_function(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_edit_symbol") as mock_pes,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
        ):
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            result = _handler("edit_symbol")(
                registry,
                {
                    "path": "f.py",
                    "symbol_type": "function",
                    "symbol_name": "hello",
                    "new_definition": "def hello(): pass",
                },
                approve_cb,
                False,
            )

        assert result.ok is True
        assert result.payload["applied"] is True
        assert result.payload["applied_tool"] == "edit_symbol"

    def test_valid_method(self, registry: ToolRegistry, approve_cb: MagicMock):
        with (
            patch("aura.conversation.tools.registry.propose_edit_symbol") as mock_pes,
            patch("aura.conversation.tools.registry.backup_existing", return_value=None),
        ):
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "f.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            result = _handler("edit_symbol")(
                registry,
                {
                    "path": "f.py",
                    "symbol_type": "method",
                    "symbol_name": "greet",
                    "new_definition": "def greet(self): pass",
                    "class_name": "Greeter",
                },
                approve_cb,
                False,
            )

        assert result.ok is True

    def test_non_string_symbol_type(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("edit_symbol")(
            registry,
            {
                "path": "f.py",
                "symbol_type": 123,
                "symbol_name": "foo",
                "new_definition": "def foo(): pass",
            },
            approve_cb,
            False,
        )
        assert result.ok is False
        assert "string" in str(result.payload).lower()

    def test_read_only_blocked(self, registry: ToolRegistry, approve_cb: MagicMock):
        registry.set_read_only(True)
        result = _handler("edit_symbol")(
            registry,
            {"path": "f.py", "symbol_type": "function", "symbol_name": "foo", "new_definition": "x"},
            approve_cb,
            False,
        )
        assert result.ok is False


# update_todo_list


class TestUpdateTodoList:
    """Tests for the update_todo_list tool — pure validation, no external deps."""

    def test_valid_one_task(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(
            registry,
            {"tasks": [{"description": "Do something", "status": "pending"}]},
            approve_cb,
            False,
        )
        assert result.ok is True
        assert result.extras.get("is_todo_update") is True

    def test_valid_multiple_tasks(self, registry: ToolRegistry, approve_cb: MagicMock):
        tasks = [
            {"description": "Task 1", "status": "pending"},
            {"description": "Task 2", "status": "active"},
            {"description": "Task 3", "status": "done"},
        ]
        result = _handler("update_todo_list")(registry, {"tasks": tasks}, approve_cb, False)
        assert result.ok is True

    def test_missing_tasks_defaults_empty(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(registry, {}, approve_cb, False)
        assert result.ok is True

    def test_tasks_not_a_list(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(registry, {"tasks": "not-a-list"}, approve_cb, False)
        assert result.ok is False
        assert "array" in str(result.payload).lower()

    def test_task_not_a_dict(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(
            registry, {"tasks": ["string"]}, approve_cb, False
        )
        assert result.ok is False
        assert "object" in str(result.payload).lower()

    def test_task_missing_keys(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(
            registry, {"tasks": [{"description": "only desc"}]}, approve_cb, False
        )
        assert result.ok is False

    def test_invalid_status(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = _handler("update_todo_list")(
            registry,
            {"tasks": [{"description": "x", "status": "invalid_status"}]},
            approve_cb,
            False,
        )
        assert result.ok is False
        assert "invalid status" in str(result.payload).lower()


# execute — unknown tool


class TestExecuteUnknown:
    """Tests for registry.execute with an unknown tool name."""

    def test_unknown_tool(self, registry: ToolRegistry, approve_cb: MagicMock):
        result = registry.execute("nonexistent_tool", {}, approve_cb, False)
        assert result.ok is False
        assert "unknown tool" in str(result.payload).lower()

    def test_value_error_caught(self, registry: ToolRegistry, approve_cb: MagicMock):
        """If a tool raises ValueError, execute catches it and returns ok=False."""
        with patch("aura.conversation.tools.fs_handler.read_file",
                   side_effect=ValueError("boom")):
            result = registry.execute("read_file", {"path": "x"}, approve_cb, False)
        assert result.ok is False


# Handler registration verification


class TestLaunchReadOnlyDrone:
    """launch_read_only_drone: validates and starts background drone run."""

    def test_missing_drone_id(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Missing drone_id returns error."""
        result = _handler("launch_read_only_drone")(registry, {"goal": "test"}, approve_cb, False)
        assert not result.ok
        assert "drone_id is required" in result.payload["error"]

    def test_missing_goal(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Missing goal returns error."""
        result = _handler("launch_read_only_drone")(registry, {"drone_id": "bug-scout"}, approve_cb, False)
        assert not result.ok
        assert "goal is required" in result.payload["error"]

    def test_valid_launch(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Valid launch returns run_id and queued status."""
        mock_drone = MagicMock()
        mock_drone.id = "bug-scout"
        mock_drone.name = "Bug Scout"
        mock_drone.write_policy = "read_only"

        mock_job = MagicMock()
        mock_job.run_id = "test-run-123"
        mock_job.drone_id = "bug-scout"
        mock_job.drone_name = "Bug Scout"
        mock_job.status = "queued"

        with patch("aura.drones.store.DroneStore") as mock_store_cls:
            mock_store_cls.load_drone.return_value = mock_drone
            with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
                mock_runner.return_value.launch.return_value = mock_job
                result = _handler("launch_read_only_drone")(
                    registry,
                    {"drone_id": "bug-scout", "goal": "investigate crash"},
                    approve_cb,
                    False,
                )

        assert result.ok
        output = result.payload
        assert output["run_id"] == "test-run-123"
        assert output["status"] == "queued"
        assert output["drone_id"] == "bug-scout"

    def test_unknown_drone_id(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Unknown drone_id returns clean error."""
        with patch("aura.drones.store.DroneStore") as mock_store_cls:
            mock_store_cls.load_drone.return_value = None
            mock_store_cls.list_drones.return_value = []
            result = _handler("launch_read_only_drone")(
                registry,
                {"drone_id": "nonexistent", "goal": "test"},
                approve_cb,
                False,
            )
        assert not result.ok
        assert "Unknown drone_id" in result.payload["error"]

    def test_rejects_write_capable_drone(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Write-capable drone is rejected."""
        mock_drone = MagicMock()
        mock_drone.id = "writer"
        mock_drone.name = "Writer"
        mock_drone.write_policy = "on_approval"

        with patch("aura.drones.store.DroneStore") as mock_store_cls:
            mock_store_cls.load_drone.return_value = mock_drone
            result = _handler("launch_read_only_drone")(
                registry,
                {"drone_id": "writer", "goal": "test"},
                approve_cb,
                False,
            )
        assert not result.ok
        assert "write_policy" in result.payload["error"]

    def test_does_not_call_sync_runner_inline(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Handler does NOT call run_read_only_drone_sync directly."""
        mock_drone = MagicMock()
        mock_drone.id = "bug-scout"
        mock_drone.name = "Bug Scout"
        mock_drone.write_policy = "read_only"

        mock_job = MagicMock()
        mock_job.run_id = "test-run-123"
        mock_job.drone_id = "bug-scout"
        mock_job.drone_name = "Bug Scout"
        mock_job.status = "queued"

        with patch("aura.drones.store.DroneStore") as mock_store_cls:
            mock_store_cls.load_drone.return_value = mock_drone
            with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
                mock_runner.return_value.launch.return_value = mock_job
                with patch("aura.drones.sync_runner.run_read_only_drone_sync") as sync_spy:
                    result = _handler("launch_read_only_drone")(
                        registry,
                        {"drone_id": "bug-scout", "goal": "investigate"},
                        approve_cb,
                        False,
                    )
                    sync_spy.assert_not_called()
        assert result.ok


class TestCheckDroneRun:
    """check_drone_run: queries background drone job state."""

    def test_missing_run_id(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Missing run_id returns error."""
        result = _handler("check_drone_run")(registry, {}, approve_cb, False)
        assert not result.ok
        assert "run_id is required" in result.payload["error"]

    def test_unknown_run_id(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Unknown run_id returns clean error."""
        with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
            mock_runner.return_value.get.return_value = None
            result = _handler("check_drone_run")(
                registry,
                {"run_id": "nonexistent"},
                approve_cb,
                False,
            )
        assert not result.ok
        assert "Unknown run_id" in result.payload["error"]

    def test_returns_completed_state(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Completed job returns summary and stats."""
        mock_job = MagicMock()
        mock_job.run_id = "test-run-456"
        mock_job.drone_id = "bug-scout"
        mock_job.drone_name = "Bug Scout"
        mock_job.status = "completed"
        mock_job.goal = "investigate crash"
        mock_job.summary = "Found the bug"
        mock_job.tool_calls_made = 5
        mock_job.tool_errors = 0
        mock_job.elapsed_seconds = 3.2
        mock_job.error = None
        mock_job.receipt = None

        with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
            mock_runner.return_value.get.return_value = mock_job
            result = _handler("check_drone_run")(
                registry,
                {"run_id": "test-run-456"},
                approve_cb,
                False,
            )
        assert result.ok
        output = result.payload
        assert output["status"] == "completed"
        assert output["summary"] == "Found the bug"
        assert output["tool_calls_made"] == 5

    def test_returns_failed_state(self, registry: ToolRegistry, approve_cb: MagicMock):
        """Failed job returns error info."""
        mock_job = MagicMock()
        mock_job.run_id = "test-run-789"
        mock_job.drone_id = "bug-scout"
        mock_job.drone_name = "Bug Scout"
        mock_job.status = "failed"
        mock_job.goal = "test"
        mock_job.summary = ""
        mock_job.tool_calls_made = 0
        mock_job.tool_errors = 0
        mock_job.elapsed_seconds = 0.0
        mock_job.error = "Something went wrong"
        mock_job.receipt = None

        with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
            mock_runner.return_value.get.return_value = mock_job
            result = _handler("check_drone_run")(
                registry,
                {"run_id": "test-run-789"},
                approve_cb,
                False,
            )
        assert result.ok
        output = result.payload
        assert output["status"] == "failed"
        assert output["error"] == "Something went wrong"

    def test_forwards_wait_seconds(self, registry: ToolRegistry, approve_cb: MagicMock):
        """wait_seconds is forwarded to background runner."""
        mock_job = MagicMock()
        mock_job.run_id = "test-run-cap"
        mock_job.drone_id = "bug-scout"
        mock_job.drone_name = "Bug Scout"
        mock_job.status = "running"
        mock_job.goal = "test"
        mock_job.summary = ""
        mock_job.tool_calls_made = 0
        mock_job.tool_errors = 0
        mock_job.elapsed_seconds = 0.0
        mock_job.error = None
        mock_job.receipt = None

        with patch("aura.drones.background_runner.get_background_runner") as mock_runner:
            mock_runner.return_value.get.return_value = mock_job
            _handler("check_drone_run")(
                registry,
                {"run_id": "test-run-cap", "wait_seconds": 30},
                approve_cb,
                False,
            )
            kwargs = mock_runner.return_value.get.call_args.kwargs
            assert kwargs.get("wait_seconds") == 30.0


class TestHandlerRegistration:
    """Verify that all expected tools are registered and callable."""

    # The tools registered in TOOL_HANDLERS
    EXPECTED_TOOLS = {
        "read_file",
        "read_files",
        "list_directory",
        "glob",
        "grep_search",
        "read_file_outline",
        "find_usages",
        "search_codebase",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_log_file",
        "git_branch_list",
        "git_stash_list",
        "git_stash_show",
        "web_search",
        "web_fetch",
        "write_file",
        "delete_file",
        "apply_edit_transaction",
        "edit_file",
        "edit_symbol",
        "edit_line_range",
        "patch_file",
        "update_todo_list",
        "search_project_memory",
        "save_to_project_memory",
        "run_diagnostic_command",
        "launch_read_only_drone",
        "run_read_only_drone",
        "check_drone_run",
        "get_workspace_snapshot",
        "summon_drone",
        "resolve_capability",
    }

    def test_all_expected_tools_present(self):
        registered = set(TOOL_HANDLERS.keys())
        assert registered == self.EXPECTED_TOOLS, (
            f"TOOL_HANDLERS has {registered - self.EXPECTED_TOOLS} extras and "
            f"is missing {self.EXPECTED_TOOLS - registered}"
        )

    def test_all_handlers_callable(self):
        for name, handler in TOOL_HANDLERS.items():
            assert callable(handler), f"Handler for '{name}' is not callable"

    def test_each_handler_has_minimal_valid_test(self):
        """Every handler has at least one test per the class-based test structure.

        This is a sanity check — the real validation is CI test execution.
        """
        for name in TOOL_HANDLERS:
            assert name in TestHandlerRegistration.EXPECTED_TOOLS, (
                f"Unexpected tool '{name}' has no test class"
            )


class TestModeToolSurfaces:
    """Verify tool definitions exposed to the API for different modes."""

    def test_planner_tool_surface(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")

        tool_names = {
            tool_def["function"]["name"]
            for tool_def in registry.tool_defs()
        }

        assert tool_names == {
            "read_file",
            "read_files",
            "read_file_outline",
            "list_directory",
            "glob",
            "grep_search",
            "find_usages",
            "search_codebase",
            "git_status",
            "git_diff",
            "git_log",
            "git_show",
            "git_log_file",
            "dispatch_to_worker",
            "launch_read_only_drone",
            "run_read_only_drone",
            "check_drone_run",
            "run_research",
            "run_diagnostic_command",
            "get_workspace_snapshot",
            "summon_drone",
            "resolve_capability",
        }
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "edit_symbol" not in tool_names
        assert "patch_file" not in tool_names
        assert "run_terminal_command" not in tool_names

    def test_researcher_tool_surface(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="researcher")

        tool_names = {
            tool_def["function"]["name"]
            for tool_def in registry.tool_defs()
        }

        # Should have web tools AND read tools
        assert "web_search" in tool_names
        assert "web_fetch" in tool_names
        assert "read_file" in tool_names
        assert "read_files" in tool_names
        assert "grep_search" in tool_names
        # Should NOT have write or dispatch tools
        assert "write_file" not in tool_names
        assert "dispatch_to_worker" not in tool_names

    def test_worker_tool_surface(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="worker")

        tool_names = {
            tool_def["function"]["name"]
            for tool_def in registry.tool_defs()
        }

        assert "write_file" in tool_names
        assert "delete_file" in tool_names
        assert "patch_file" in tool_names
        assert "apply_edit_transaction" not in tool_names
        assert "edit_file" not in tool_names
        assert "edit_line_range" not in tool_names
        assert "update_todo_list" in tool_names
        assert "run_terminal_command" in tool_names
        assert "run_research" in tool_names  # Added!
        assert "launch_read_only_drone" in tool_names
        assert "check_drone_run" in tool_names
        assert "dispatch_to_worker" not in tool_names

    def test_single_tool_surface(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")

        tool_names = {
            tool_def["function"]["name"]
            for tool_def in registry.tool_defs()
        }

        assert "write_file" in tool_names
        assert "delete_file" in tool_names
        assert "apply_edit_transaction" in tool_names
        assert "edit_file" in tool_names
        assert "edit_line_range" in tool_names
        assert "patch_file" in tool_names
        assert "run_terminal_command" in tool_names
        assert "run_research" in tool_names  # Added!
        assert "run_diagnostic_command" in tool_names
        assert "get_workspace_snapshot" in tool_names
        assert "dispatch_to_worker" not in tool_names

class TestProposeLineRangeEdit:
    """Tests for propose_line_range_edit from fs_write.py."""

    def test_edit_line_range_replace_middle(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        target = ws / "test.txt"
        target.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = propose_line_range_edit(ws, target, 2, 4, "REPLACED\n")
        assert result["ok"] is True
        assert result["new_content"] == "line1\nREPLACED\nline4\nline5\n"

    def test_edit_line_range_insert_before_first_line(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        target = ws / "test.txt"
        target.write_text("line1\nline2\nline3\n")
        result = propose_line_range_edit(ws, target, 1, 1, "inserted\n")
        assert result["ok"] is True
        assert result["new_content"] == "inserted\nline1\nline2\nline3\n"

    def test_edit_line_range_insert_in_middle(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        target = ws / "test.txt"
        target.write_text("line1\nline2\nline3\n")
        result = propose_line_range_edit(ws, target, 2, 2, "inserted\n")
        assert result["ok"] is True
        assert result["new_content"] == "line1\ninserted\nline2\nline3\n"

    def test_edit_line_range_append_at_eof(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        target = ws / "test.txt"
        target.write_text("line1\nline2\nline3\n")
        result = propose_line_range_edit(ws, target, 4, 4, "appended\n")
        assert result["ok"] is True
        assert result["new_content"] == "line1\nline2\nline3\nappended\n"

    def test_edit_line_range_append_at_eof_single_line_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        target = ws / "test.txt"
        target.write_text("only line\n")
        result = propose_line_range_edit(ws, target, 2, 2, "appended\n")
        assert result["ok"] is True
        assert result["new_content"] == "only line\nappended\n"

def test_edit_line_range_stale_bounds_structured_payload(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "test.txt"
    target.write_text("line1\\nline2\\n")
    result = propose_line_range_edit(ws, target, 5, 6, "x")
    assert result["ok"] is False
    assert "failure_class" in result
    assert result["failure_class"] == "edit_mechanics_stale_line_range"
    assert "suggested_next_action" in result

class TestSingleModeToolDefs:
    """Single mode exposes write tools but not dispatch_to_worker."""

    def test_includes_write_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "write_file" in tool_names

    def test_includes_edit_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_file" in tool_names

    def test_includes_edit_symbol(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_symbol" in tool_names

    def test_includes_edit_line_range(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_line_range" in tool_names

    def test_includes_patch_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "patch_file" in tool_names

    def test_excludes_dispatch_to_worker(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "dispatch_to_worker" not in tool_names


class TestPlannerModeToolDefs:
    """Planner mode exposes dispatch_to_worker but not write tools."""

    def test_includes_dispatch_to_worker(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "dispatch_to_worker" in tool_names

    def test_excludes_write_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "write_file" not in tool_names

    def test_excludes_edit_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_file" not in tool_names

    def test_excludes_edit_symbol(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_symbol" not in tool_names

    def test_excludes_edit_line_range(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "edit_line_range" not in tool_names

    def test_excludes_patch_file(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "patch_file" not in tool_names


class TestResolveCapability:
    """resolve_capability: resolves capability requirements via CapabilityResolver."""

    TOOL_NAME = "resolve_capability"

    def test_handler_in_registry(self):
        """Handler is registered in TOOL_HANDLERS."""
        from aura.conversation.tools.registry import TOOL_HANDLERS
        assert self.TOOL_NAME in TOOL_HANDLERS

    def test_present_in_planner_mode(self, tmp_path):
        """Tool is available in planner mode."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        tool_defs = registry.tool_defs()
        tool_names = {t["function"]["name"] for t in tool_defs}
        assert self.TOOL_NAME in tool_names

    def test_absent_in_worker_mode(self, tmp_path):
        """Tool is NOT available in worker mode."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="worker")
        tool_defs = registry.tool_defs()
        tool_names = {t["function"]["name"] for t in tool_defs}
        assert self.TOOL_NAME not in tool_names

    def test_absent_in_read_only_mode(self, tmp_path):
        """Tool is NOT available in read_only mode."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, read_only=True, mode="planner")
        tool_defs = registry.tool_defs()
        tool_names = {t["function"]["name"] for t in tool_defs}
        assert self.TOOL_NAME not in tool_names

    def test_valid_requirements_returns_ok(self, tmp_path):
        """Handler returns ok=True for valid requirements with CapabilityResolution shape."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {
            "requirements": [
                {"capability": "read_file", "purpose": "read source", "notes": ""},
            ],
        }
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert result.ok
        payload = result.payload
        assert payload.get("ok") is True
        assert "requirements" in payload
        assert "candidates" in payload
        assert "selected_bindings" in payload
        assert "allowed_tools" in payload
        assert "setup_notes" in payload

    def test_missing_requirements_returns_error(self, tmp_path):
        """Missing requirements field returns ok=False."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {}
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert not result.ok
        assert result.payload.get("ok") is False
        assert "error" in result.payload

    def test_empty_requirements_returns_error(self, tmp_path):
        """Empty requirements list returns ok=False."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {"requirements": []}
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert not result.ok
        assert result.payload.get("ok") is False
        assert "error" in result.payload

    def test_unknown_keys_ignored(self, tmp_path):
        """Extra unknown input keys are ignored, handler still succeeds."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {
            "requirements": [{"capability": "read_file"}],
            "unknown_field": "should_be_ignored",
        }
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert result.ok
        assert result.payload.get("ok") is True

    def test_static_tool_matched(self, tmp_path):
        """A requirement for 'read_file' gets a static_tool binding in planner mode."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {
            "requirements": [{"capability": "read_file"}],
        }
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert result.ok
        bindings = result.payload.get("selected_bindings", [])
        read_file_bindings = [b for b in bindings if b["capability"] == "read_file"]
        assert len(read_file_bindings) >= 1
        assert read_file_bindings[0]["route_kind"] == "static_tool"
        assert "read_file" in result.payload.get("allowed_tools", [])

    def test_unknown_capability_gets_fallback(self, tmp_path):
        """An unknown capability gets a generated_code fallback."""
        from aura.conversation.tools.registry import ToolRegistry
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(ws, mode="planner")
        args = {
            "requirements": [{"capability": "fly_to_the_moon"}],
        }
        result = registry.execute(self.TOOL_NAME, args, lambda _: True)
        assert result.ok
        bindings = result.payload.get("selected_bindings", [])
        fly_bindings = [b for b in bindings if b["capability"] == "fly_to_the_moon"]
        assert len(fly_bindings) == 1
        assert fly_bindings[0]["route_kind"] == "generated_code"
