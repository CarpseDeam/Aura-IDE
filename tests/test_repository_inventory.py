"""Canonical repository-file inventory: discovery, policy, and metadata.

``aura.repository_inventory`` is the single owner of "what counts as a
repository file" for every repository-aware subsystem (BM25, CodeIntel,
repo-map freshness). These tests pin its discovery sources (Git-aware vs.
bounded filesystem fallback), the shared skip/sensitive policy, the workspace
jail, the metadata-only contract (stat, never file bodies), and truthful
partial/truncated reporting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from aura.repository_inventory import (
    build_inventory,
    is_sensitive_path,
    passes_canonical_policy,
)
import aura.repository_inventory as inventory_module


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _init_git_repo(root: Path) -> None:
    _git(["init"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)


def _write(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _rel_paths(root: Path) -> set[str]:
    return set(build_inventory(root).rel_paths())


# ── Git-aware discovery ──────────────────────────────────────────────────


class TestGitBackedDiscovery:
    def test_includes_tracked_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _write(tmp_path / "app.py")
        _git(["add", "app.py"], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        inv = build_inventory(tmp_path)
        assert inv.source == "git"
        assert "app.py" in inv.rel_paths()

    def test_includes_non_ignored_untracked_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _write(tmp_path / "committed.py")
        _git(["add", "committed.py"], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        # Never staged or committed, but not gitignored either.
        _write(tmp_path / "untracked.py")

        rel = _rel_paths(tmp_path)
        assert "committed.py" in rel
        assert "untracked.py" in rel

    def test_excludes_gitignored_files(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _write(tmp_path / ".gitignore", "ignored.py\n")
        _git(["add", ".gitignore"], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        _write(tmp_path / "ignored.py")
        _write(tmp_path / "kept.py")

        rel = _rel_paths(tmp_path)
        assert "ignored.py" not in rel
        assert "kept.py" in rel


# ── filesystem fallback ──────────────────────────────────────────────────


class TestFilesystemFallback:
    def test_non_git_workspace_uses_filesystem_discovery(self, tmp_path: Path) -> None:
        _write(tmp_path / "app.py")

        inv = build_inventory(tmp_path)
        assert inv.source == "filesystem"
        assert inv.complete is True
        assert "app.py" in inv.rel_paths()

    def test_reports_partial_state_under_a_controlled_file_budget(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        for i in range(5):
            _write(tmp_path / f"f{i}.py", f"x = {i}\n")

        monkeypatch.setattr(inventory_module, "MAX_FILES_CONSIDERED", 2)

        inv = build_inventory(tmp_path)
        assert inv.source == "filesystem"
        assert inv.complete is False
        assert inv.incomplete_reason == "file cap reached"
        # Truncated, not silently claiming the whole repository.
        assert len(inv.files) < 5

    def test_reports_partial_state_under_a_controlled_dir_budget(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        for i in range(5):
            _write(tmp_path / f"sub{i}" / "f.py")

        monkeypatch.setattr(inventory_module, "MAX_DIRS_VISITED", 1)

        inv = build_inventory(tmp_path)
        assert inv.complete is False
        assert inv.incomplete_reason == "directory cap reached"

    def test_complete_discovery_reports_complete_true(self, tmp_path: Path) -> None:
        _write(tmp_path / "app.py")
        inv = build_inventory(tmp_path)
        assert inv.complete is True
        assert inv.incomplete_reason is None


# ── sensitive-file policy ────────────────────────────────────────────────


class TestSensitiveFileExclusion:
    def test_env_is_excluded_from_inventory(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env", "SECRET=1\n")
        _write(tmp_path / "app.py")

        rel = _rel_paths(tmp_path)
        assert ".env" not in rel
        assert "app.py" in rel

    def test_private_key_is_excluded_from_inventory(self, tmp_path: Path) -> None:
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        _write(ssh / "id_ed25519", "-----BEGIN-----\n")

        rel = _rel_paths(tmp_path)
        assert ".ssh/id_ed25519" not in rel

    def test_env_example_template_is_permitted(self, tmp_path: Path) -> None:
        """Matches existing grep policy: templates are not secrets."""
        _write(tmp_path / ".env.example", "API_KEY=changeme\n")

        # Hidden-file pruning also applies to top-level dotfiles generally,
        # so check the policy function directly (this is what grep's
        # is_sensitive_path/search_scope contract pins) rather than requiring
        # dotfiles to be full inventory candidates.
        assert is_sensitive_path(".env.example") is False
        assert is_sensitive_path(".env") is True

    def test_sensitive_exclusion_also_applies_under_git_discovery(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        _write(tmp_path / "app.py")
        _write(tmp_path / ".env", "SECRET=1\n")
        _git(["add", "-A"], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        inv = build_inventory(tmp_path)
        assert inv.source == "git"
        assert ".env" not in inv.rel_paths()
        assert "app.py" in inv.rel_paths()


# ── workspace jail ───────────────────────────────────────────────────────


class TestWorkspaceJail:
    def test_symlink_escaping_workspace_is_excluded(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        _write(outside / "secret.py", "leaked = True\n")

        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write(workspace / "app.py")

        link = workspace / "escape.py"
        try:
            link.symlink_to(outside / "secret.py")
        except OSError:
            pytest.skip("symlink creation not permitted in this environment")

        rel = _rel_paths(workspace)
        assert "app.py" in rel
        assert "escape.py" not in rel


# ── canonical skip policy ────────────────────────────────────────────────


class TestCanonicalSkipPolicy:
    def test_skip_directories_are_excluded(self, tmp_path: Path) -> None:
        _write(tmp_path / "app.py")
        _write(tmp_path / "__pycache__" / "app.cpython-313.pyc", "junk")
        _write(tmp_path / "node_modules" / "pkg" / "index.js", "junk")
        _write(tmp_path / ".venv" / "lib" / "site.py", "junk")

        rel = _rel_paths(tmp_path)
        assert rel == {"app.py"}

    def test_passes_canonical_policy_matches_inventory_for_a_skip_dir(self) -> None:
        assert passes_canonical_policy("node_modules/pkg/index.js") is False
        assert passes_canonical_policy("app.py") is True

    def test_hidden_files_are_excluded(self, tmp_path: Path) -> None:
        _write(tmp_path / ".hidden_config")
        _write(tmp_path / "app.py")

        rel = _rel_paths(tmp_path)
        assert ".hidden_config" not in rel
        assert "app.py" in rel


# ── path normalisation and metadata ─────────────────────────────────────


class TestPathsAndMetadata:
    def test_relative_paths_are_normalised_posix(self, tmp_path: Path) -> None:
        _write(tmp_path / "sub" / "nested.py")

        rel = _rel_paths(tmp_path)
        assert "sub/nested.py" in rel
        assert not any("\\" in p for p in rel)

    def test_exposes_size_and_mtime_without_reading_file_body(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        _write(target, "x = 1\n" * 10)
        before = time.time() - 5
        after = time.time() + 5

        inv = build_inventory(tmp_path)
        rf = next(f for f in inv.files if f.rel_path == "app.py")

        # Ground truth is the real on-disk stat, not a platform-dependent
        # recomputation of the string's encoded length (Windows text-mode
        # writes translate "\n" to "\r\n").
        assert rf.size == target.stat().st_size
        assert before <= rf.mtime <= after
