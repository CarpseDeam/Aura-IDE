"""Tests for aura.git_ops — git integration, snapshots, and checkpoints."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aura.git_ops import (
    _AURA_GITIGNORE_ENTRIES,
    commit_changed_files,
    commit_diff,
    ensure_aura_gitignored,
    git_init,
    is_git_repo,
    recent_commits,
    restore_to_snapshot,
    snapshot,
    undo_last_commit,
    working_tree_status,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class MockResult:
    """Simulates subprocess.CompletedProcess for testing.

    Attributes match those that the production code accesses (returncode,
    stdout, stderr).
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_run(side_effects):
    """Build a ``subprocess.run`` mock that yields from *side_effects*.

    Each element of *side_effects* is either:
      - A ``MockResult`` instance — returned directly (or turned into a
        ``CalledProcessError`` if ``check=True`` and ``returncode != 0``).
      - An ``Exception`` instance — raised directly.

    When ``check=True`` and the result's ``returncode != 0`` the mock raises
    ``subprocess.CalledProcessError`` with ``stderr``/``stdout`` left as
    strings when ``text=True`` was passed (matching real subprocess behaviour),
    or encoded to bytes when ``text`` was not passed.
    """
    calls = list(reversed(side_effects))

    def _run(*args, **kwargs):
        item = calls.pop()
        if isinstance(item, BaseException):
            raise item
        if kwargs.get("check") and item.returncode != 0:
            cmd = args[0] if args else kwargs.get("cmd", [])
            is_text = kwargs.get("text", False)
            stderr_val = item.stderr
            stdout_val = item.stdout
            if not is_text:
                if isinstance(stderr_val, str):
                    stderr_val = stderr_val.encode()
                if isinstance(stdout_val, str):
                    stdout_val = stdout_val.encode()
            raise subprocess.CalledProcessError(
                item.returncode, cmd, output=stdout_val, stderr=stderr_val,
            )
        return item

    return _run


# ===================================================================
# TestIsGitRepo
# ===================================================================


class TestIsGitRepo:
    """is_git_repo() — check whether a directory is inside a git working tree."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return True when git rev-parse succeeds."""
        monkeypatch.setattr(subprocess, "run", _make_run([MockResult()]))
        assert is_git_repo(tmp_path) is True

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when CalledProcessError is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.CalledProcessError(1, ["git", "rev-parse"])]),
        )
        assert is_git_repo(tmp_path) is False

    def test_file_not_found(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when FileNotFoundError is raised."""
        monkeypatch.setattr(
            subprocess, "run", _make_run([FileNotFoundError("no git")]),
        )
        assert is_git_repo(tmp_path) is False

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when TimeoutExpired is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git", 5)]),
        )
        assert is_git_repo(tmp_path) is False


# ===================================================================
# TestCheckpointHelpers
# ===================================================================


class TestCheckpointHelpers:
    """Checkpoint history helpers."""

    def test_recent_commits_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is False
        assert commits == []
        assert msg == "Not a git repository."

    def test_recent_commits_parses_log_records(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        stdout = (
            "\x1eabc123def\x1fabc123d\x1fAdd thing\x1fAda\x1f2 hours ago\n"
            "a.txt\n"
            "b.txt\n"
            "\x1edef456ghi\x1fdef456g\x1fFix thing\x1fGrace\x1fyesterday\n"
            "src/main.py\n"
        )
        monkeypatch.setattr(subprocess, "run", _make_run([MockResult(stdout=stdout)]))

        ok, commits, msg = recent_commits(tmp_path, limit=2)

        assert ok is True
        assert msg == ""
        assert commits == [
            {
                "sha": "abc123def",
                "short_sha": "abc123d",
                "subject": "Add thing",
                "author": "Ada",
                "relative_date": "2 hours ago",
                "changed_files_count": 2,
                "changed_files": ["a.txt", "b.txt"],
            },
            {
                "sha": "def456ghi",
                "short_sha": "def456g",
                "subject": "Fix thing",
                "author": "Grace",
                "relative_date": "yesterday",
                "changed_files_count": 1,
                "changed_files": ["src/main.py"],
            },
        ]

    def test_recent_commits_reports_git_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(returncode=128, stderr="fatal: bad revision")]),
        )
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is False
        assert commits == []
        assert msg == "fatal: bad revision"

    def test_recent_commits_empty_repo(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([
                MockResult(
                    returncode=128,
                    stderr="fatal: your current branch does not have any commits yet",
                ),
            ]),
        )
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is True
        assert commits == []
        assert msg == ""

    def test_commit_changed_files_success(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout="a.txt\nsrc/main.py\n")]),
        )
        ok, files, msg = commit_changed_files(tmp_path, "abc123")
        assert ok is True
        assert files == ["a.txt", "src/main.py"]
        assert msg == ""

    def test_commit_diff_success(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout="commit abc123\n+hello\n")]),
        )
        ok, diff, msg = commit_diff(tmp_path, "abc123")
        assert ok is True
        assert diff == "commit abc123\n+hello\n"
        assert msg == ""

    def test_working_tree_status_success(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout=" M aura/git_ops.py\n")]),
        )
        ok, status, msg = working_tree_status(tmp_path)
        assert ok is True
        assert status == " M aura/git_ops.py\n"
        assert msg == ""


# ===================================================================
# TestUndoLastCommit
# ===================================================================


class TestUndoLastCommit:
    """undo_last_commit() — soft-reset HEAD~1, keeping changes staged."""

    def test_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when not a git repo."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Not a git repository."

    def test_no_commits(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when rev-list reports 0 commits."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="0\n")]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "No commits to undo."

    def test_rev_list_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when rev-list raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                subprocess.CalledProcessError(1, ["git", "rev-list", "--count", "HEAD"]),
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_rev_list_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when rev-list times out."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git rev-list", 5)]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_value_error_parse(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when rev-list stdout is not a valid integer."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="not_a_number\n")]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when reset --soft HEAD~1 succeeds."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout="5\n"),    # rev-list — 5 commits
                MockResult(),                 # reset --soft HEAD~1 — OK
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is True
        assert msg == "Undo complete — last commit reverted, changes are staged."

    def test_reset_fails(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout="5\n"),           # rev-list — OK
                MockResult(returncode=1, stderr="some error"),
                # reset raises CalledProcessError (check=True + rc != 0)
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "git reset failed: some error"


# ===================================================================
# TestSnapshot
# ===================================================================


class TestSnapshot:
    """snapshot() — capture the current HEAD SHA."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return the SHA when rev-parse returns it."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="abc123def\n", returncode=0)]),
        )
        result = snapshot(tmp_path)
        assert result == "abc123def"

    def test_empty_stdout(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when stdout is empty (no HEAD yet)."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="")]),
        )
        assert snapshot(tmp_path) is None

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when rev-parse raises CalledProcessError."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"]),
            ]),
        )
        assert snapshot(tmp_path) is None

    def test_file_not_found(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when FileNotFoundError is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        assert snapshot(tmp_path) is None

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when TimeoutExpired is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git rev-parse", 5)]),
        )
        assert snapshot(tmp_path) is None


# ===================================================================
# TestRestoreToSnapshot
# ===================================================================


class TestRestoreToSnapshot:
    """restore_to_snapshot() — hard-reset to a given SHA."""

    def test_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when not a git repo."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "Not a git repository."

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when git reset --hard succeeds."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult()]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is True
        assert msg == "Restored to abc123de."

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(returncode=1, stderr="fatal: invalid ref"),
            ]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "git reset failed: fatal: invalid ref"

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset times out."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git reset", 10)]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "git reset timed out."


# ===================================================================
# TestGitInit
# ===================================================================


class TestGitInit:
    """git_init() — initialize a git repo and create an initial commit."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when all operations succeed."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(returncode=0),    # git commit — OK
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete — initial commit created."

    def test_git_init_fails_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when git init raises CalledProcessError."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(returncode=1, stderr="permission denied"),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git init failed: permission denied"

    def test_git_init_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git init times out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git init", 10)]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git init timed out."

    def test_nothing_to_commit(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when commit stderr contains 'nothing to commit'."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(                  # git commit — "nothing to commit"
                    returncode=1,
                    stderr="nothing to commit, working tree clean",
                ),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete (no files to commit yet)."

    def test_commit_fails_other_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when commit fails with a non-'nothing to commit' error."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(                  # git commit — other error
                    returncode=1,
                    stderr="error: failed to commit",
                ),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git commit failed: error: failed to commit"

    def test_git_add_fails_but_continues(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Proceed to commit even if git add -A fails (continues silently)."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                            # git init — OK
                subprocess.CalledProcessError(           # git add -A — fails (caught)
                    1, ["git", "add", "-A"]
                ),
                MockResult(returncode=0),                # git commit — OK
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete — initial commit created."


# ===================================================================
# TestEnsureAuraGitignored
# ===================================================================


class TestEnsureAuraGitignored:
    """ensure_aura_gitignored() — manage explicit .gitignore allowlist."""

    def test_no_gitignore_creates_all_entries(self, tmp_path: Path) -> None:
        """Create .gitignore with all _AURA_GITIGNORE_ENTRIES when none exists."""
        gitignore = tmp_path / ".gitignore"
        assert not gitignore.exists()
        ensure_aura_gitignored(tmp_path)
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        expected = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
        assert content == expected

    def test_broad_aura_slash_removed(self, tmp_path: Path) -> None:
        """.gitignore with `.aura/` gets broad line removed and entries added."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        expected = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
        assert content == expected

    def test_broad_dot_aura_removed(self, tmp_path: Path) -> None:
        """.gitignore with `.aura` gets broad line removed and entries added."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        expected = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
        assert content == expected

    def test_broad_root_aura_slash_removed(self, tmp_path: Path) -> None:
        """.gitignore with `/.aura/` gets broad line removed and entries added."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("/.aura/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        expected = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
        assert content == expected

    def test_specific_entries_kept_and_not_duplicated(self, tmp_path: Path) -> None:
        """Existing specific entry kept, only missing entries appended."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("/.aura/backups/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        entries = set(content.strip().splitlines())
        assert "/.aura/backups/" in entries
        for e in _AURA_GITIGNORE_ENTRIES:
            assert e in entries
        assert content.count("/.aura/backups/") == 1

    def test_all_entries_present_idempotent(self, tmp_path: Path) -> None:
        """All entries already present — no change."""
        gitignore = tmp_path / ".gitignore"
        original = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
        gitignore.write_text(original, encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == original

    def test_unrelated_content_preserved(self, tmp_path: Path) -> None:
        """Unrelated gitignore lines survive unchanged."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert "__pycache__/" in content
        for e in _AURA_GITIGNORE_ENTRIES:
            assert e in content

    def test_negation_pattern_not_removed(self, tmp_path: Path) -> None:
        """Negation pattern like `!.aura/drones/` is kept (not a broad ignore)."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("!.aura/drones/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert "!.aura/drones/" in content
        for e in _AURA_GITIGNORE_ENTRIES:
            assert e in content

    def test_broad_drones_slash_removed(self, tmp_path: Path) -> None:
        """Ignoring all `.aura/drones/` would hide user-authored Drone definitions."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("/.aura/drones/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert "/.aura/drones/" not in lines
        assert "/.aura/drones/runs/" in lines

    def test_aura_star_removed(self, tmp_path: Path) -> None:
        """.gitignore with `.aura/*` gets broad line removed and entries added."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura/*\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert ".aura/*" not in content
        for e in _AURA_GITIGNORE_ENTRIES:
            assert e in content

    def test_file_no_trailing_newline(self, tmp_path: Path) -> None:
        """Append correctly when .gitignore doesn't end with newline."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content.startswith("*.pyc\n")
        for e in _AURA_GITIGNORE_ENTRIES:
            assert e in content

    def test_os_error_on_read_is_silent(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Silently return when reading .gitignore raises OSError."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("some content\n", encoding="utf-8")
        original_content = "some content\n"

        def _broken_read_text(_self: object, *args: object, **kwargs: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _broken_read_text)
        ensure_aura_gitignored(tmp_path)
        monkeypatch.undo()
        assert gitignore.read_text(encoding="utf-8") == original_content
