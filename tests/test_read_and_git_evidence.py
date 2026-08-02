"""Read and Git results are evidence, so they have to be literally true.

A read payload is what the model reasons from and what ``patch_file`` accepts
as proof a file is unchanged, and a git path is something the next tool call
will try to open. These tests pin the properties that makes possible: the
hash describes the bytes that were returned, decoding is uniform, truncation
reports itself honestly, and a git path is a real path inside the workspace.
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from aura.config import MAX_READ_BYTES, get_subprocess_kwargs
from aura.conversation.tools.fs_read import read_file, read_file_range
from aura.conversation.tools.git_tools import git_status

# ── read evidence ───────────────────────────────────────────────────────────


class TestReadSnapshotIsCoherent:

    def test_hash_and_size_describe_the_file_that_was_read(self, tmp_path):
        body = "line one\nline two\n".encode("utf-8")
        target = tmp_path / "a.py"
        target.write_bytes(body)

        result = read_file(tmp_path, target)
        assert result["ok"] is True
        assert result["content_hash"] == hashlib.sha256(body).hexdigest()
        assert result["file_size"] == len(body)
        assert result["content"].encode("utf-8") == body

    def test_hash_covers_the_whole_file_even_when_truncated(self, tmp_path):
        """The hash is the file's version, and patch_file accepts it as proof
        the file has not changed — it must cover every byte, not just the
        bytes that fit in the read limit."""
        body = b"x" * (MAX_READ_BYTES + 5000)
        target = tmp_path / "big.py"
        target.write_bytes(body)

        result = read_file(tmp_path, target)
        assert result["truncated"] is True
        assert result["file_size"] == len(body)
        assert result["content_hash"] == hashlib.sha256(body).hexdigest()

    def test_range_and_whole_file_reads_agree_on_the_hash(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes("a\nb\nc\nd\n".encode("utf-8"))
        assert (
            read_file_range(tmp_path, target, 2, 3)["content_hash"]
            == read_file(tmp_path, target)["content_hash"]
        )


class TestTruncationIsTruthful:

    def test_a_multibyte_character_at_the_cut_does_not_break_the_read(self, tmp_path):
        """Slicing bytes then decoding strictly made any large file with a
        single non-ASCII character report itself as not being UTF-8 — the file
        became permanently unreadable."""
        body = b"x" * (MAX_READ_BYTES - 1) + "é".encode("utf-8") + b"tail"
        target = tmp_path / "big.py"
        target.write_bytes(body)

        result = read_file(tmp_path, target)
        assert result["ok"] is True, result.get("error")
        assert result["truncated"] is True

    def test_the_returned_text_is_never_mojibake(self, tmp_path):
        body = b"x" * (MAX_READ_BYTES - 1) + "é".encode("utf-8") + b"tail"
        target = tmp_path / "big.py"
        target.write_bytes(body)

        content = read_file(tmp_path, target)["content"]
        assert "�" not in content

    def test_truncation_reports_what_was_returned_and_what_exists(self, tmp_path):
        body = b"a\n" * MAX_READ_BYTES
        target = tmp_path / "big.py"
        target.write_bytes(body)

        result = read_file(tmp_path, target)
        assert result["truncated"] is True
        assert result["returned_bytes"] <= MAX_READ_BYTES
        assert result["returned_bytes"] < result["file_size"]
        assert result["returned_lines"] > 0

    def test_an_untruncated_read_says_so(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"one\ntwo\n")
        result = read_file(tmp_path, target)
        assert result["truncated"] is False
        assert result["returned_bytes"] == result["file_size"] == 8
        assert result["returned_lines"] == 2

    def test_a_final_line_without_a_newline_still_counts(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"one\ntwo")
        assert read_file(tmp_path, target)["returned_lines"] == 2


class TestDecodingIsUniform:

    def test_both_readers_reject_the_same_binary_file(self, tmp_path):
        """read_file_range used to substitute U+FFFD, so a binary file came
        back as noise that read like real content while read_file refused it."""
        target = tmp_path / "bin.dat"
        target.write_bytes(b"\xff\xfe\x00\x01 hello")

        assert read_file(tmp_path, target)["ok"] is False
        assert read_file_range(tmp_path, target, 1, 5)["ok"] is False

    def test_a_range_read_preserves_non_ascii_exactly(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes("a\nb\né\nd\n".encode("utf-8"))
        result = read_file_range(tmp_path, target, 3, 3)
        assert result["content"] == "é\n"

    def test_a_multibyte_character_spanning_read_chunks_survives(self, tmp_path):
        """The range reader streams in 1 MiB chunks; a character straddling a
        chunk boundary must not be decoded as two broken halves."""
        filler = ("x" * 1023 + "\n") * 1024          # just over 1 MiB
        target = tmp_path / "big.py"
        target.write_bytes((filler + "é\n").encode("utf-8"))

        total = read_file_range(tmp_path, target, 1, 1)["total_lines"]
        result = read_file_range(tmp_path, target, total, total)
        assert result["content"] == "é\n"
        assert "�" not in result["content"]

    def test_total_lines_counts_a_missing_final_newline(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"one\ntwo")
        assert read_file_range(tmp_path, target, 1, 9)["total_lines"] == 2


# ── git evidence ────────────────────────────────────────────────────────────


def git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        **get_subprocess_kwargs(),
    )


@pytest.fixture
def repo(tmp_path):
    if git(["--version"], tmp_path).returncode != 0:
        pytest.skip("git is not installed")
    git(["init", "-q", "."], tmp_path)
    git(["config", "user.email", "a@b.c"], tmp_path)
    git(["config", "user.name", "test"], tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "café.txt").write_text("x", encoding="utf-8")
    (sub / "with space.txt").write_text("x", encoding="utf-8")
    (tmp_path / "normal.txt").write_text("x", encoding="utf-8")
    git(["add", "-A"], tmp_path)
    git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


class TestGitPathsAreReal:

    def test_a_non_ascii_path_is_not_escaped(self, repo):
        """Default porcelain escapes it to "sub/caf\\303\\251.txt" — a name
        that does not exist, so the next read_file on it fails."""
        (repo / "sub" / "café.txt").write_text("changed", encoding="utf-8")
        result = git_status(repo)

        assert result["ok"] is True
        assert "sub/café.txt" in result["unstaged"]

    def test_every_reported_path_exists_on_disk(self, repo):
        (repo / "sub" / "café.txt").write_text("changed", encoding="utf-8")
        (repo / "sub" / "with space.txt").write_text("changed", encoding="utf-8")
        (repo / "brand new.txt").write_text("new", encoding="utf-8")
        result = git_status(repo)

        reported = result["staged"] + result["unstaged"] + result["untracked"]
        assert reported
        for rel in reported:
            assert (repo / rel).exists(), f"git reported a path that does not exist: {rel}"

    def test_a_path_with_spaces_keeps_its_spaces(self, repo):
        (repo / "sub" / "with space.txt").write_text("changed", encoding="utf-8")
        assert "sub/with space.txt" in git_status(repo)["unstaged"]

    def test_a_rename_reports_the_new_path_not_the_old(self, repo):
        git(["mv", "normal.txt", "renamed.txt"], repo)
        result = git_status(repo)

        assert "renamed.txt" in result["staged"]
        assert "normal.txt" not in result["staged"]

    def test_a_rename_does_not_shift_the_following_entry(self, repo):
        """A rename occupies two NUL-delimited fields; miscounting them makes
        the *next* file's status codes come from the old path."""
        git(["mv", "normal.txt", "renamed.txt"], repo)
        (repo / "sub" / "café.txt").write_text("changed", encoding="utf-8")
        result = git_status(repo)

        assert "renamed.txt" in result["staged"]
        assert "sub/café.txt" in result["unstaged"]


class TestGitPathsAreJailed:

    def test_paths_are_relative_to_the_workspace_not_the_repo(self, repo):
        """Porcelain output is always repo-root-relative. When the workspace
        is a subdirectory, those paths do not resolve from the workspace."""
        (repo / "sub" / "café.txt").write_text("changed", encoding="utf-8")
        result = git_status(repo / "sub")

        assert result["ok"] is True
        assert "café.txt" in result["unstaged"]
        for rel in result["unstaged"]:
            assert (repo / "sub" / rel).exists()

    def test_changes_outside_the_workspace_are_not_reported(self, repo):
        (repo / "normal.txt").write_text("changed outside", encoding="utf-8")
        result = git_status(repo / "sub")

        assert "normal.txt" not in result["unstaged"]
        assert "../normal.txt" not in result["unstaged"]

    def test_dropped_paths_are_counted_not_silently_lost(self, repo):
        """Otherwise a workspace with no changes and a repo full of them look
        identical."""
        (repo / "normal.txt").write_text("changed outside", encoding="utf-8")
        result = git_status(repo / "sub")

        assert result["outside_workspace"] >= 1

    def test_a_clean_workspace_is_still_reported_clean(self, repo):
        result = git_status(repo / "sub")
        assert result["ok"] is True
        assert result["clean"] is True

    def test_branch_information_survives_the_z_format(self, repo):
        result = git_status(repo)
        assert result["ok"] is True
        assert result["branch"]
        assert result["ahead"] == 0 and result["behind"] == 0
