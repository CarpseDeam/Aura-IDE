"""Tests for TypeScriptSyntaxProbe with mocked subprocess calls."""
from __future__ import annotations

import json
import textwrap
from unittest.mock import patch

from aura.syntax_probe.typescript_probe import TypeScriptSyntaxProbe

_TS_VALID = textwrap.dedent("""\
    const x: number = 42;
    console.log(x);
""")

_TS_INVALID = textwrap.dedent("""\
    const x: number = 42;
    console.log(x
""")


class TestTypeScriptSyntaxProbe:
    """Tests for TypeScriptSyntaxProbe."""

    def _probe(self) -> TypeScriptSyntaxProbe:
        return TypeScriptSyntaxProbe()

    # --- clean parse -> pass ---

    def test_clean_parse_returns_pass(self, tmp_path) -> None:
        probe = self._probe()
        ts_file = tmp_path / "valid.ts"
        ts_file.write_text(_TS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({"diagnostics": []})
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.ts")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False
        assert result.toolchain_available is True

    # --- parse diagnostics -> fail ---

    def test_parse_diagnostics_returns_fail(self, tmp_path) -> None:
        probe = self._probe()
        ts_file = tmp_path / "invalid.ts"
        ts_file.write_text(_TS_INVALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({
                "diagnostics": [
                    {
                        "line": 2,
                        "column": 12,
                        "messageText": "Expression expected.",
                    },
                ],
            })
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "invalid.ts")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 2
        assert result.column == 12
        assert "Expression expected" in result.error
        assert result.failure_class == "syntax_invalid"
        assert result.toolchain_available is True

    # --- missing Node -> no_evidence ---

    def test_missing_node_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        ts_file = tmp_path / "valid.ts"
        ts_file.write_text(_TS_VALID)
        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "valid.ts")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert result.toolchain_available is False

    # --- node available but typescript package missing -> no_evidence ---

    def test_missing_typescript_package_returns_no_evidence(
        self, tmp_path,
    ) -> None:
        probe = self._probe()
        ts_file = tmp_path / "valid.ts"
        ts_file.write_text(_TS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = (
                "Error: Cannot find module 'typescript'\n"
            )
            result = probe.check(tmp_path, "valid.ts")
        assert result.evidence == "no_evidence"
        assert result.toolchain_available is True

    # --- outside-workspace safety ---

    def test_outside_workspace_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        outside_dir = tmp_path.parent / "_outside_tmp_ts"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.ts"
        outside_file.write_text(_TS_VALID)
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
        result = probe.check(tmp_path, "../outside_workspace.ts")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        result = probe.check(tmp_path, "nonexistent.ts")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- ambiguous output -> no_evidence ---

    def test_ambiguous_output_returns_no_evidence(self, tmp_path) -> None:
        probe = self._probe()
        ts_file = tmp_path / "valid.ts"
        ts_file.write_text(_TS_VALID)
        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json at all"
            mock_run.return_value.stderr = ""
            result = probe.check(tmp_path, "valid.ts")
        assert result.evidence == "no_evidence"

    # --- detect ---

    def test_detect_ts(self) -> None:
        assert TypeScriptSyntaxProbe.detect("foo.ts") is True
        assert TypeScriptSyntaxProbe.detect("foo.tsx") is True
        assert TypeScriptSyntaxProbe.detect("foo.mts") is True
        assert TypeScriptSyntaxProbe.detect("foo.cts") is True
        assert TypeScriptSyntaxProbe.detect("foo.js") is False
        assert TypeScriptSyntaxProbe.detect("foo.json") is False
