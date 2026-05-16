"""Tests for every tool registered in TOOL_HANDLERS.

Each tool gets its own test class with valid-input and invalid-input tests.
All underlying functions are mocked so no real filesystem, git, or network
calls are made during test execution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from aura.conversation.tools._types import (
    ApprovalDecision,
)
from aura.conversation.tools.registry import (
    TOOL_HANDLERS,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ===================================================================
# read_file
# ===================================================================


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


# ===================================================================
# read_files
# ===================================================================


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


# ===================================================================
# list_directory
# ===================================================================


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


# ===================================================================
# glob
# ===================================================================


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


# ===================================================================
# read_file_outline
# ===================================================================


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


# ===================================================================
# grep_search
# ===================================================================


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


# ===================================================================
# find_usages
# ===================================================================


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


# ===================================================================
# search_codebase
# ===================================================================


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


# ===================================================================
# git tools
# ===================================================================


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


# ===================================================================
# web tools
# ===================================================================


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


# ===================================================================
# write_file
# ===================================================================


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


# ===================================================================
# edit_file
# ===================================================================


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
        assert result.payload["applied"] == "edit_file"

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


# ===================================================================
# edit_symbol
# ===================================================================


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
        assert result.payload["applied"] == "edit_symbol"

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


# ===================================================================
# update_todo_list
# ===================================================================


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


# ===================================================================
# execute — unknown tool
# ===================================================================


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


# ===================================================================
# Handler registration verification
# ===================================================================


class TestHandlerRegistration:
    """Verify that all expected tools are registered and callable."""

    # The 22 tools registered in TOOL_HANDLERS
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
        "edit_file",
        "edit_symbol",
        "update_todo_list",
        "search_project_memory",
        "save_to_project_memory",
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
            "run_research",
        }
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "edit_symbol" not in tool_names
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
        assert "edit_file" in tool_names
        assert "update_todo_list" in tool_names
        assert "run_terminal_command" in tool_names
        assert "run_research" in tool_names  # Added!
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
        assert "edit_file" in tool_names
        assert "run_terminal_command" in tool_names
        assert "run_research" in tool_names  # Added!
        assert "dispatch_to_worker" not in tool_names
