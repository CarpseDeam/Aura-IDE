from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from aura.syntax_probe.rust_probe import RustSyntaxProbe


_PRIMARY_ERROR_JSON = json.dumps({
    "reason": "compiler-message",
    "package_id": "test@0.1.0",
    "target": {"kind": ["lib"], "name": "test"},
    "message": {
        "level": "error",
        "message": "expected `;`",
        "rendered": "error[E0308]: expected `;`\n --> src/main.rs:2:13\n",
        "spans": [
            {
                "file_name": "/fake/src/main.rs",
                "is_primary": True,
                "line_start": 2,
                "line_end": 2,
                "column_start": 13,
                "column_end": 13,
            }
        ],
    },
})

_OTHER_FILE_ERROR_JSON = json.dumps({
    "reason": "compiler-message",
    "package_id": "test@0.1.0",
    "target": {"kind": ["lib"], "name": "test"},
    "message": {
        "level": "error",
        "message": "cannot find `foo` in this scope",
        "rendered": "error[E0425]: cannot find `foo`\n",
        "spans": [
            {
                "file_name": "/fake/src/lib.rs",
                "is_primary": True,
                "line_start": 3,
                "line_end": 3,
                "column_start": 5,
                "column_end": 5,
            }
        ],
    },
})


class TestRustSyntaxProbe:
    """Tests for the Rust syntax probe."""

    def test_detect_rs_file(self) -> None:
        assert RustSyntaxProbe.detect("main.rs") is True
        assert RustSyntaxProbe.detect("src/lib.rs") is True

    def test_detect_non_rs_file(self) -> None:
        assert RustSyntaxProbe.detect("main.py") is False
        assert RustSyntaxProbe.detect("Cargo.toml") is False

    def test_no_cargo_toml_returns_no_evidence(self, tmp_path) -> None:
        probe = RustSyntaxProbe()
        rs_file = tmp_path / "main.rs"
        rs_file.write_text("fn main() {}\n")
        result = probe.check(tmp_path, "main.rs")
        assert result.evidence == "no_evidence"

    def test_missing_cargo_command_returns_no_evidence(self, tmp_path) -> None:
        probe = RustSyntaxProbe()
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = \"test\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
        )
        (tmp_path / "main.rs").write_text("fn main() {}\n")

        with patch("shutil.which", return_value=None):
            result = probe.check(tmp_path, "main.rs")

        assert result.evidence == "no_evidence"
        assert result.toolchain_available is False

    def test_parser_returns_pass_on_no_errors(self) -> None:
        probe = RustSyntaxProbe()
        fake_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        result = probe._parse_cargo_output(fake_proc, "/fake/main.rs")
        assert result.evidence == "pass"

    def test_parser_extracts_fail_for_target_file(self) -> None:
        probe = RustSyntaxProbe()
        fake_proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_PRIMARY_ERROR_JSON + "\n",
            stderr="",
        )
        result = probe._parse_cargo_output(fake_proc, "/fake/src/main.rs")
        assert result.evidence == "fail"
        assert result.failure_class == "syntax_invalid"
        assert result.line == 2
        assert result.column == 13

    def test_parser_ignores_errors_for_other_files(self) -> None:
        probe = RustSyntaxProbe()
        fake_proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_OTHER_FILE_ERROR_JSON + "\n",
            stderr="",
        )
        result = probe._parse_cargo_output(fake_proc, "/fake/src/main.rs")
        assert result.evidence == "no_evidence"
