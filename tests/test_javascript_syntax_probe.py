"""Tests for JavaScriptSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import textwrap
from unittest.mock import patch

from aura.syntax_probe.javascript_probe import JavaScriptSyntaxProbe

_JS_VALID = textwrap.dedent("""\
    const x = 42;
    console.log(x);
""")

_JS_INVALID = textwrap.dedent("""\
    const x = 42;
    console.log(x
""")


class TestJavaScriptSyntaxProbe:
    """Tests for JavaScriptSyntaxProbe."""

    def _probe(self) -> JavaScriptSyntaxProbe:
        return JavaScriptSyntaxProbe()

    # --- valid JS -> pass ---

    def test_valid_js_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        js_file = tmp_path / "valid.js"
        js_file.write_text(_JS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.js")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- syntax error -> fail ---

    def test_syntax_error_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        js_file = tmp_path / "invalid.js"
        js_file.write_text(_JS_INVALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "SyntaxError: Unexpected token (2:5)\n"
            )
            result = probe.check(tmp_path, "invalid.js")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 2
        assert result.column == 5
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    def test_syntax_error_with_at_format(self, tmp_path) -> None:
        """Node errors can use 'at <file>:line:col' format."""
        probe = self._probe()
        js_file = tmp_path / "test.js"
        js_file.write_text(_JS_INVALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "SyntaxError: Unexpected identifier\n"
                f"    at {js_file}:3:10\n"
            )
            result = probe.check(tmp_path, "test.js")
        assert result.evidence == "fail"
        assert result.line == 3
        assert result.column == 10

    # --- missing Node -> no_evidence ---

    def test_missing_node_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        js_file = tmp_path / "valid.js"
        js_file.write_text(_JS_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.js")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- ambiguous nonzero output -> no_evidence ---

    def test_ambiguous_error_returns_no_evidence(self, tmp_path) -> None:
        """Runtime errors that aren't parse errors should yield no_evidence."""
        probe = self._probe()
        js_file = tmp_path / "throw.js"
        js_file.write_text(_JS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "ReferenceError: x is not defined\n"
            )
            result = probe.check(tmp_path, "throw.js")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_js"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.js"
        outside_file.write_text(_JS_VALID)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "../outside_workspace.js")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.js")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- TimeoutExpired -> no_evidence ---

    def test_timeout_returns_no_evidence(self, tmp_path) -> None:
        import subprocess
        probe = self._probe()
        js_file = tmp_path / "slow.js"
        js_file.write_text(_JS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 cmd="node --check", timeout=30,
             )):
            result = probe.check(tmp_path, "slow.js")
        assert result.evidence == "no_evidence"

    # --- detect ---

    def test_detect_js(self) -> None:
        assert JavaScriptSyntaxProbe.detect("foo.js") is True
        assert JavaScriptSyntaxProbe.detect("foo.mjs") is True
        assert JavaScriptSyntaxProbe.detect("foo.cjs") is True
        assert JavaScriptSyntaxProbe.detect("foo.ts") is False
        assert JavaScriptSyntaxProbe.detect("foo.json") is False
