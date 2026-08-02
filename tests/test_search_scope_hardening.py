"""The search tools cannot read outside the workspace or read secrets.

``grep_search`` and ``find_usages`` are the widest read surface in the
catalog: they take a pattern rather than a path, so nothing in the call names
the files whose contents come back. These tests pin the two properties that
makes safe — the workspace is a jail, and credential files inside it are
skipped and *reported* rather than silently searched.

Both engines are exercised for every property. Which engine runs depends on
whether ``rg`` happens to be installed, and that must never change what is
reachable.
"""

from __future__ import annotations

import threading

import pytest

from aura.conversation.tools import grep as grep_module
from aura.conversation.tools.find_usages import find_usages
from aura.conversation.tools.grep import grep_files
from aura.conversation.tools.search_scope import (
    is_sensitive_path,
    resolve_include_scope,
)

NEEDLE = "ZZTOPSECRETZZ"


@pytest.fixture(params=["ripgrep", "python"])
def engine(request, monkeypatch):
    """Run each test against both search engines."""
    if request.param == "python":
        monkeypatch.setattr(grep_module.shutil, "which", lambda name: None)
    elif grep_module.shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    return request.param


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(f"value = '{NEEDLE}'\n", encoding="utf-8")
    (ws / ".env").write_text(f"API_KEY={NEEDLE}\n", encoding="utf-8")
    (ws / ".env.example").write_text(f"API_KEY={NEEDLE}\n", encoding="utf-8")
    (ws / "server.pem").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (ws / "deploy.key").write_text(f"{NEEDLE}\n", encoding="utf-8")
    ssh = ws / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (ssh / "id_ed25519.pub").write_text(f"{NEEDLE}\n", encoding="utf-8")
    # Outside the workspace entirely.
    (tmp_path / "OUTSIDE.txt").write_text(f"{NEEDLE}\n", encoding="utf-8")
    return ws


def matched_paths(result) -> set[str]:
    return {m["path"] for m in result.get("matches", [])}


def skipped_paths(result) -> set[str]:
    return {s["path"] for s in result.get("skipped_details", [])}


# ── the workspace is a jail ─────────────────────────────────────────────────


class TestWorkspaceJail:

    def test_an_absolute_include_pattern_is_rejected(self, engine, workspace, tmp_path):
        """``root / include_pattern`` silently discards ``root`` when the
        pattern is absolute — the classic way a scope becomes a location."""
        result = grep_files(
            workspace, NEEDLE, regex_mode=False,
            include_pattern=str(tmp_path / "OUTSIDE.txt"),
        )
        assert result["ok"] is False
        assert result["failure_class"] == "search_scope_escape"
        assert result.get("matches") in (None, [])

    def test_a_parent_traversal_include_pattern_is_rejected(self, engine, workspace):
        result = grep_files(
            workspace, NEEDLE, regex_mode=False, include_pattern="../OUTSIDE.txt",
        )
        assert result["ok"] is False
        assert result["failure_class"] == "search_scope_escape"

    def test_a_nested_traversal_is_rejected(self, engine, workspace):
        result = grep_files(
            workspace, NEEDLE, regex_mode=False, include_pattern="sub/../../OUTSIDE.txt",
        )
        assert result["ok"] is False
        assert result["failure_class"] == "search_scope_escape"

    def test_a_plain_sweep_never_reaches_outside_the_workspace(self, engine, workspace):
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        assert result["ok"] is True
        for path in matched_paths(result):
            assert not path.startswith(".."), f"{path} escaped the workspace"

    def test_rejection_is_an_error_not_a_silent_workspace_wide_search(
        self, engine, workspace, tmp_path,
    ):
        """Clamping an out-of-workspace scope back to the root would answer a
        question nobody asked, and look like a real result."""
        result = grep_files(
            workspace, NEEDLE, regex_mode=False,
            include_pattern=str(tmp_path / "OUTSIDE.txt"),
        )
        assert result["ok"] is False
        assert not result.get("matches")

    def test_find_usages_inherits_the_jail(self, engine, workspace):
        result = find_usages(workspace, NEEDLE, include_pattern="../OUTSIDE.txt")
        assert result["ok"] is False
        assert result["failure_class"] == "search_scope_escape"


# ── secrets inside the workspace are not search material ────────────────────


class TestSensitivePaths:

    @pytest.mark.parametrize(
        "secret", [".env", "server.pem", "deploy.key", ".ssh/id_ed25519"],
    )
    def test_a_secret_is_never_matched(self, engine, workspace, secret):
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        assert result["ok"] is True
        assert secret not in matched_paths(result)

    def test_ordinary_source_still_matches(self, engine, workspace):
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        assert "app.py" in matched_paths(result)

    def test_the_skip_is_reported_not_hidden(self, engine, workspace):
        """A silently dropped file reads as 'no secrets here', which is a
        different and worse answer than 'not searched'."""
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        skipped = skipped_paths(result)
        assert ".env" in skipped
        assert "server.pem" in skipped
        assert result["skipped_files"] >= 2
        reasons = {s["reason"] for s in result["skipped_details"]}
        assert any("sensitive" in r for r in reasons)

    def test_a_template_env_file_is_still_searchable(self, engine, workspace):
        """``.env.example`` is committed on purpose; treating it as a secret
        would hide ordinary configuration documentation."""
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        assert ".env.example" in matched_paths(result)

    def test_a_public_key_is_not_treated_as_private(self, engine, workspace):
        assert is_sensitive_path("keys/id_ed25519.pub") is False

    def test_naming_the_secret_directly_does_not_bypass_the_policy(
        self, engine, workspace,
    ):
        """The policy is on the file, not on how the file was reached."""
        result = grep_files(
            workspace, NEEDLE, regex_mode=False, include_pattern=".env",
        )
        assert ".env" not in matched_paths(result)

    def test_searched_files_does_not_count_skipped_files(self, engine, workspace):
        result = grep_files(workspace, NEEDLE, regex_mode=False, max_results=500)
        assert result["searched_files"] >= 1
        assert result["searched_files"] + result["skipped_files"] >= len(
            matched_paths(result)
        )


class TestSensitivePolicyUnit:

    @pytest.mark.parametrize("path", [
        ".env", ".env.local", ".env.production", "cfg/.env",
        "certs/server.pem", "a/b/deploy.key", "secrets.p12", "store.jks",
        "home/.ssh/config", ".gnupg/secring.gpg", ".aws/credentials",
        ".aura/secrets/provider.json", ".aura/tokens/relay.json",
    ])
    def test_sensitive(self, path):
        assert is_sensitive_path(path) is True

    @pytest.mark.parametrize("path", [
        "app.py", ".env.example", ".env.sample", ".env.template",
        "keys/id_rsa.pub", "README.md", "environment.py", "docs/pem.md",
    ])
    def test_not_sensitive(self, path):
        assert is_sensitive_path(path) is False

    def test_windows_separators_are_normalised(self):
        assert is_sensitive_path(r"cfg\.env") is True

    def test_case_insensitive(self):
        assert is_sensitive_path("CERTS/SERVER.PEM") is True


# ── scope resolution unit contract ──────────────────────────────────────────


class TestResolveIncludeScope:

    def test_no_pattern_is_no_scope(self, tmp_path):
        assert resolve_include_scope(tmp_path, None) == (None, None, None)

    def test_an_existing_file_resolves_to_an_exact_target(self, tmp_path):
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        exact, glob, error = resolve_include_scope(tmp_path, "a.py")
        assert error is None and glob is None
        assert [p.name for p in exact] == ["a.py"]

    def test_a_glob_stays_a_glob(self, tmp_path):
        exact, glob, error = resolve_include_scope(tmp_path, "*.py")
        assert error is None and exact is None and glob == "*.py"

    def test_a_windows_absolute_path_is_rejected(self, tmp_path):
        _, _, error = resolve_include_scope(tmp_path, r"C:\Windows\win.ini")
        assert error is not None

    def test_a_posix_absolute_path_is_rejected(self, tmp_path):
        _, _, error = resolve_include_scope(tmp_path, "/etc/passwd")
        assert error is not None


# ── the search ends when the turn ends ──────────────────────────────────────


class TestCancellation:

    def test_a_cancelled_search_reports_cancellation(self, engine, workspace):
        cancel = threading.Event()
        cancel.set()
        result = grep_files(
            workspace, NEEDLE, regex_mode=False, cancel_event=cancel,
        )
        assert result["ok"] is False
        assert result["failure_class"] == "search_cancelled"

    def test_an_uncancelled_search_is_unaffected(self, engine, workspace):
        result = grep_files(
            workspace, NEEDLE, regex_mode=False, cancel_event=threading.Event(),
        )
        assert result["ok"] is True
        assert "app.py" in matched_paths(result)

    def test_ripgrep_has_a_wall_clock_ceiling(self):
        """A turn the user cannot end is worse than a search that gives up."""
        assert grep_module.RIPGREP_TIMEOUT_SECONDS > 0
