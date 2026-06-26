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

from aura.conversation.tools.grep import _looks_like_regex, _should_skip, grep_files
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

    def test_default_regex_search_finds_plain_text(self, tmp_workspace: Path) -> None:
        """Find 'hello' in scripts/smoke.py with regex mode enabled by default."""
        result = grep_files(tmp_workspace, "hello")
        assert result["ok"] is True
        assert result["engine"] == "python"
        assert result["regex_mode"] is True
        assert result["auto_regex_retry"] is False
        assert len(result["matches"]) >= 1
        assert any("hello" in m["line"] for m in result["matches"])

    def test_case_sensitive(self, tmp_workspace: Path) -> None:
        """Case-sensitive 'Hello' should match nothing when files have 'hello'."""
        result = grep_files(tmp_workspace, "Hello", case_sensitive=True)
        assert result["ok"] is True
        assert len(result["matches"]) == 0

    def test_regex_search_finds_pattern_with_anchors(self, tmp_workspace: Path) -> None:
        """Regex pattern with ^ and $ anchors matches a full line."""
        result = grep_files(tmp_workspace, r"^VALUE = 42$", regex_mode=True)
        assert result["ok"] is True
        assert result["regex_mode"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "aura/config.py"

    def test_regex_looking_pattern_uses_regex_by_default(self, tmp_workspace: Path) -> None:
        """Regex-looking patterns use normal grep behavior by default."""
        result = grep_files(tmp_workspace, r"^VALUE = 42$")
        assert result["ok"] is True
        assert result["auto_regex_retry"] is False
        assert result["regex_mode"] is True
        assert len(result["matches"]) == 1
        assert "regex_hint" not in result

    def test_escaped_function_pattern_is_detected_as_regex(self, tmp_path: Path) -> None:
        file_path = tmp_path / "module.py"
        file_path.write_text("def foo(bar):\n    return bar\n", encoding="utf-8")

        result = grep_files(tmp_path, r"def foo\(")

        assert _looks_like_regex(r"def foo\(") is True
        assert result["ok"] is True
        assert result["auto_regex_retry"] is False
        assert result["regex_mode"] is True
        assert len(result["matches"]) == 1
        assert "regex_hint" not in result

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

    def test_include_pattern_recursive_python_only(self, tmp_workspace: Path) -> None:
        """include_pattern='**/*.py' restricts search to Python files only."""
        result = grep_files(tmp_workspace, "VALUE", include_pattern="**/*.py")
        assert result["ok"] is True
        assert len(result["matches"]) >= 1
        assert result["include_pattern"] == "**/*.py"
        for m in result["matches"]:
            assert m["path"].endswith(".py")

    def test_include_pattern_exact_file_path(self, tmp_workspace: Path) -> None:
        """include_pattern can scope the search to one exact workspace file."""
        result = grep_files(tmp_workspace, "hello", include_pattern="scripts/smoke.py")
        assert result["ok"] is True
        assert result["searched_files"] == 1
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "scripts/smoke.py"

    def test_skip_dirs_and_hidden(self, tmp_workspace: Path) -> None:
        """Hidden files and .git/ contents are not searched."""
        # .hidden_file contains "secret" but SHOULD be searched now
        result = grep_files(tmp_workspace, "secret")
        assert result["ok"] is True
        assert len(result["matches"]) == 1

    def test_binary_file_skipped(self, tmp_path: Path) -> None:
        """Binary files that can't be decoded as UTF-8 are counted as skipped."""
        binary = tmp_path / "data.bin"
        binary.write_bytes(b"\x80\x81\x82")
        readable = tmp_path / "readable.txt"
        readable.write_text("hello world\n", encoding="utf-8")

        result = grep_files(tmp_path, "hello")
        assert result["ok"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "readable.txt"
        assert result["searched_files"] == 1
        assert result["skipped_files"] == 1

    def test_skipped_large_and_binary_files_are_counted(self, tmp_path: Path) -> None:
        (tmp_path / "large.txt").write_text("0123456789abcdef", encoding="utf-8")
        (tmp_path / "binary.bin").write_bytes(b"\x80\x81\x82")
        (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")

        result = grep_files(tmp_path, "needle")

        assert result["ok"] is True
        assert result["searched_files"] == 2
        assert result["skipped_files"] == 1
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "small.txt"
        assert result["skipped_details"] == []

    def test_grep_finds_private_method_names(self, tmp_path: Path) -> None:
        target = tmp_path / "gui"
        target.mkdir()
        file_path = target / "main_window.py"
        file_path.write_text(
            "class MainWindow:\n"
            "    def _on_send(self):\n"
            "        pass\n"
            "\n"
            "    def _handle_send_text(self):\n"
            "        pass\n",
            encoding="utf-8",
        )

        result = grep_files(
            workspace_root=tmp_path,
            pattern="_on_send",
            include_pattern="**/*.py",
        )

        assert result["ok"] is True
        assert result["matches"]
        assert result["matches"][0]["path"] == "gui/main_window.py"

    def test_python_grep_does_not_skip_arbitrary_dot_paths(self, tmp_path: Path) -> None:
        dot_dir = tmp_path / ".custom"
        dot_dir.mkdir()
        file_path = dot_dir / "example.py"
        file_path.write_text("needle = True\n", encoding="utf-8")

        result = grep_files(
            workspace_root=tmp_path,
            pattern="needle",
            regex_mode=False,
            case_sensitive=False,
            max_results=10,
            include_pattern="**/*.py",
        )

        assert result["ok"] is True
        assert result["matches"]
        assert result["matches"][0]["path"] == ".custom/example.py"

    def test_python_fallback_searches_beyond_old_candidate_cap(self, tmp_path: Path) -> None:
        for index in range(150):
            (tmp_path / f"file_{index:03d}.txt").write_text("nothing here\n", encoding="utf-8")
        (tmp_path / "zzz_target.txt").write_text("needle late in traversal\n", encoding="utf-8")

        result = grep_files(tmp_path, "needle", max_results=2)

        assert result["ok"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "zzz_target.txt"
        assert result["searched_files"] == 151

    def test_skipped_details_for_binary_and_permission(self, tmp_path: Path) -> None:
        (tmp_path / "binary.bin").write_bytes(b"\x80\x81\x82")
        large_file = tmp_path / "large.txt"
        large_file.write_text("line\n" * 50000 + "target_needle\n", encoding="utf-8")

        result = grep_files(tmp_path, "target_needle")

        assert result["ok"] is True
        assert result["searched_files"] == 1
        assert result["skipped_files"] == 1
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "large.txt"
        assert result["skipped_details"] == []

    def test_no_match_reports_searched_files(self, tmp_workspace: Path) -> None:
        result = grep_files(tmp_workspace, "definitely absent")

        assert result["ok"] is True
        assert result["matches"] == []
        assert result["searched_files"] > 0
        assert "searched_files=" in result["summary"]


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

    @staticmethod
    def _rg_json_summary(searches: int) -> str:
        return json.dumps({"type": "summary", "data": {"stats": {"searches": searches}}})

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
            self._rg_json_summary(5),
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
        assert result["searched_files"] == 5

    def test_no_matches(self, monkeypatch: pytest.MonkeyPatch,
                        tmp_workspace: Path) -> None:
        """rg returns exit code 1 (no matches) → empty match list."""
        stdout_lines = [
            self._rg_json_begin("README.md"),
            self._rg_json_begin("aura/config.py"),
            self._rg_json_summary(2),
        ]
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stdout="\n".join(stdout_lines) + "\n")]),
        )
        result = grep_files(tmp_workspace, "nonexistent")
        assert result["ok"] is True
        assert len(result["matches"]) == 0
        assert result["searched_files"] == 2
        assert result["auto_regex_retry"] is False

    def test_alternation_uses_regex_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_workspace: Path
    ) -> None:
        """Alternation uses regex behavior by default."""
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(returncode=1, stdout=self._rg_json_summary(5) + "\n")

        monkeypatch.setattr(subprocess, "run", _capture_run)

        result = grep_files(tmp_workspace, "VALUE|hello")

        assert result["ok"] is True
        assert result["auto_regex_retry"] is False
        assert result["regex_mode"] is True
        assert result["matches"] == []
        assert "regex_hint" not in result
        assert len(captured) == 1
        assert "--fixed-strings" not in captured[0]

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
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(stdout="\n".join(stdout_lines) + "\n")

        monkeypatch.setattr(
            subprocess, "run",
            _capture_run,
        )
        result = grep_files(tmp_workspace, "match", max_results=3)
        assert result["ok"] is True
        assert len(result["matches"]) == 3
        assert result["truncated"] is True
        assert "--max-count" not in captured[0]

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

        # Default: regex_mode=True -> --fixed-strings absent
        grep_files(tmp_workspace, "hello")
        cmd_default = captured[0]
        assert "--fixed-strings" not in cmd_default

        # Literal mode: regex_mode=False -> --fixed-strings present
        grep_files(tmp_workspace, r"he[l]+o", regex_mode=False)
        cmd_literal = captured[1]
        assert "--fixed-strings" in cmd_literal

    def test_exact_include_path_is_ripgrep_target(self, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_workspace: Path) -> None:
        """Exact include_pattern paths are passed as rg targets, not glob filters."""
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(stdout="")

        monkeypatch.setattr(subprocess, "run", _capture_run)

        grep_files(tmp_workspace, "hello", include_pattern="scripts/smoke.py")

        cmd = captured[0]
        assert "--glob" not in cmd
        idx_sep = cmd.index("--")
        assert cmd[idx_sep + 1] == "hello"
        assert Path(cmd[idx_sep + 2]) == tmp_workspace / "scripts" / "smoke.py"

    def test_ripgrep_flags_and_safety(self, monkeypatch: pytest.MonkeyPatch,
                                       tmp_workspace: Path) -> None:
        """--hidden and -- separator are present in the rg command."""
        captured: list[list[str]] = []

        def _capture_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured.append(list(cmd))  # type: ignore[arg-type]
            return MockResult(stdout="")

        monkeypatch.setattr(subprocess, "run", _capture_run)

        grep_files(tmp_workspace, "-pattern")
        cmd = captured[0]
        assert "--hidden" in cmd
        assert "--no-ignore" in cmd
        assert "!.git/" in cmd
        assert "!.venv/" in cmd
        assert "!*.import" in cmd
        assert "--" in cmd
        # Ensure -- is before the pattern
        idx_sep = cmd.index("--")
        idx_pat = cmd.index("-pattern")
        assert idx_sep < idx_pat
