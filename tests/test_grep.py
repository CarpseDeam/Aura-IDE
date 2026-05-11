"""Exhaustive unit tests for aura/conversation/tools/grep.py.

Covers _should_skip, grep_files entry point, _grep_python fallback,
and _grep_ripgrep subprocess path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aura.conversation.tools.grep import grep_files, _should_skip
from tests.helpers import MockResult, _make_run


# ===================================================================
# _should_skip tests
# ===================================================================


class TestShouldSkip:
    """_should_skip(path: Path) -> bool — file/directory filtering."""

    def test_normal_path(self) -> None:
        """Regular source files are not skipped."""
        assert _should_skip(Path("src/main.py")) is False
        assert _should_skip(Path("README.md")) is False

    def test_skip_dirs(self) -> None:
        """Paths inside SKIP_DIRS are skipped."""
        assert _should_skip(Path(".git/config")) is True
        assert _should_skip(Path("node_modules/foo/bar.js")) is True
        assert _should_skip(Path("src/__pycache__/cache.pyc")) is True

    def test_hidden_dirs(self) -> None:
        """Paths whose any component starts with '.' are skipped."""
        assert _should_skip(Path(".venv/bin/python")) is True

    def test_file_suffixes(self) -> None:
        """Files with a suffix in SKIP_FILE_SUFFIXES are skipped."""
        assert _should_skip(Path("foo.import")) is True


# ===================================================================
# grep_files entry-point tests
# ===================================================================


class TestGrepFilesEntryPoint:
    """grep_files() validation and dispatching logic."""

    def test_empty_pattern(self, tmp_workspace: Path) -> None:
        """Empty pattern immediately returns an error without dispatching."""
        result = grep_files(tmp_workspace, "")
        assert result["ok"] is False
        assert "pattern is required" in result["error"]


# ===================================================================
# _grep_python (fallback) tests
# ===================================================================


class TestGrepPython:
    """Pure-Python recursive search (used when ripgrep is unavailable)."""

    # -- helpers -------------------------------------------------------

    @pytest.fixture(autouse=True)
    def _disable_ripgrep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force grep_files into the Python fallback path."""
        monkeypatch.setattr(shutil, "which", lambda _: None)

    # -- tests ---------------------------------------------------------

    def test_basic_substring_search(self, tmp_workspace: Path) -> None:
        """Find 'hello' in scripts/smoke.py via substring matching."""
        result = grep_files(tmp_workspace, "hello")
        assert result["ok"] is True
        assert len(result["matches"]) >= 1
        assert any("hello" in m["line"] for m in result["matches"])

    def test_case_sensitive(self, tmp_workspace: Path) -> None:
        """Case-sensitive 'Hello' should match nothing when files have 'hello'."""
        result = grep_files(tmp_workspace, "Hello", case_sensitive=True)
        assert result["ok"] is True
        assert len(result["matches"]) == 0

    def test_regex_mode(self, tmp_workspace: Path) -> None:
        """Regex pattern he[l]+o matches 'hello'."""
        result = grep_files(tmp_workspace, r"he[l]+o", regex_mode=True)
        assert result["ok"] is True
        assert len(result["matches"]) >= 1
        assert any("hello" in m["line"] for m in result["matches"])

    def test_invalid_regex(self, tmp_workspace: Path) -> None:
        """Malformed regex returns an error."""
        result = grep_files(tmp_workspace, r"[invalid", regex_mode=True)
        assert result["ok"] is False
        assert "invalid regex" in result["error"]

    def test_max_results_truncation(self, tmp_path: Path) -> None:
        """When there are more hits than max_results, truncated=True and count is capped."""
        common = "shared_word"
        for i in range(5):
            p = tmp_path / f"file_{i}.txt"
            p.write_text(f"{common}\n", encoding="utf-8")

        result = grep_files(tmp_path, common, max_results=2)
        assert result["ok"] is True
        assert len(result["matches"]) <= 2
        assert result["truncated"] is True

    def test_include_pattern(self, tmp_workspace: Path) -> None:
        """include_pattern='*.md' restricts search to Markdown files only."""
        result = grep_files(tmp_workspace, "content", include_pattern="*.md")
        assert result["ok"] is True
        assert len(result["matches"]) >= 1
        for m in result["matches"]:
            assert m["path"].endswith(".md")

    def test_skip_dirs_and_hidden(self, tmp_workspace: Path) -> None:
        """Hidden files and .git/ contents are not searched."""
        # .hidden_file contains "secret" but should be skipped
        result = grep_files(tmp_workspace, "secret")
        assert result["ok"] is True
        assert len(result["matches"]) == 0

    def test_binary_file_skipped(self, tmp_path: Path) -> None:
        """Binary files that can't be decoded as UTF-8 are silently skipped."""
        binary = tmp_path / "data.bin"
        binary.write_bytes(b"\x00\xff\xfe")
        readable = tmp_path / "readable.txt"
        readable.write_text("hello world\n", encoding="utf-8")

        # Search for a pattern that only binary contains (if it were text)
        result = grep_files(tmp_path, "hello")
        assert result["ok"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "readable.txt"


# ===================================================================
# _grep_ripgrep tests
# ===================================================================


class TestGrepRipgrep:
    """Subprocess-based ripgrep backend."""

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _rg_json_begin(path: str) -> str:
        return json.dumps({"type": "begin", "data": {"path": {"text": path}}})

    @staticmethod
    def _rg_json_match(
        path: str, line_number: int, line: str, start_col: int
    ) -> str:
        return json.dumps({
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": line + "\n"},
                "line_number": line_number,
                "submatches": [{"start": start_col}],
            },
        })

    @staticmethod
    def _rg_json_end(path: str) -> str:
        return json.dumps({"type": "end", "data": {"path": {"text": path}}})

    # -- ripgrep available ---------------------------------------------

    @pytest.fixture(autouse=True)
    def _enable_ripgrep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Advertise ripgrep as available."""
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/rg")

    # -- tests ---------------------------------------------------------

    def test_parses_json_matches(self, monkeypatch: pytest.MonkeyPatch,
                                 tmp_workspace: Path) -> None:
        """Well-formed JSON-lines output from rg is correctly parsed."""
        stdout_lines = [
            self._rg_json_begin("scripts/smoke.py"),
            self._rg_json_match("scripts/smoke.py", 1, "print('hello')", 6),
            self._rg_json_end("scripts/smoke.py"),
        ]
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="\n".join(stdout_lines) + "\n")]),
        )
        result = grep_files(tmp_workspace, "hello")
        assert result["ok"] is True
        assert result["engine"] == "ripgrep"
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["path"] == "scripts/smoke.py"
        assert m["line_number"] == 1
        assert "hello" in m["line"]
        assert m["match_column"] == 6

    def test_no_matches(self, monkeypatch: pytest.MonkeyPatch,
                        tmp_workspace: Path) -> None:
        """rg returns exit code 1 (no matches) → empty match list."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stdout="")]),
        )
        result = grep_files(tmp_workspace, "nonexistent")
        assert result["ok"] is True
        assert len(result["matches"]) == 0

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch,
                                 tmp_workspace: Path) -> None:
        """rg returns code 2 (error) → error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=2, stderr="bad pattern")]),
        )
        result = grep_files(tmp_workspace, "[invalid")
        assert result["ok"] is False
        assert "ripgrep failed" in result["error"] or "bad pattern" in result["error"]

    def test_subprocess_exception(self, monkeypatch: pytest.MonkeyPatch,
                                  tmp_workspace: Path) -> None:
        """Exception during subprocess.run → error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([OSError("broken pipe")]),
        )
        result = grep_files(tmp_workspace, "hello")
        assert result["ok"] is False
        assert "ripgrep error" in result["error"]

    def test_truncation(self, monkeypatch: pytest.MonkeyPatch,
                        tmp_workspace: Path) -> None:
        """More matches in rg output than max_results → truncated=True."""
        stdout_lines = []
        for i in range(5):
            stdout_lines.append(
                self._rg_json_match("file.txt", i + 1, f"match {i}", 0)
            )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="\n".join(stdout_lines) + "\n")]),
        )
        result = grep_files(tmp_workspace, "match", max_results=3)
        assert result["ok"] is True
        assert len(result["matches"]) == 3
        assert result["truncated"] is True

    def test_case_sensitive_flag(self, monkeypatch: pytest.MonkeyPatch,
                                 tmp_workspace: Path) -> None:
        """--ignore-case is present when case_sensitive=False, absent when True."""
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(stdout="")

        monkeypatch.setattr(subprocess, "run", _capture_run)

        # Default: case_sensitive=False → --ignore-case present
        grep_files(tmp_workspace, "hello")
        cmd_default = captured[0]
        assert "--ignore-case" in cmd_default

        # Case sensitive: case_sensitive=True → --ignore-case absent
        grep_files(tmp_workspace, "Hello", case_sensitive=True)
        cmd_sensitive = captured[1]
        assert "--ignore-case" not in cmd_sensitive

    def test_regex_flag(self, monkeypatch: pytest.MonkeyPatch,
                        tmp_workspace: Path) -> None:
        """--fixed-strings is present when regex_mode=False, absent when True."""
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(stdout="")

        monkeypatch.setattr(subprocess, "run", _capture_run)

        # Default: regex_mode=False → --fixed-strings present
        grep_files(tmp_workspace, "hello")
        cmd_default = captured[0]
        assert "--fixed-strings" in cmd_default

        # Regex mode: regex_mode=True → --fixed-strings absent
        grep_files(tmp_workspace, r"he[l]+o", regex_mode=True)
        cmd_regex = captured[1]
        assert "--fixed-strings" not in cmd_regex
