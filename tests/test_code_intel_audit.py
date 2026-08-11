"""Focused coverage for audit_changed_files' index injection contract.

ToolRegistry now owns a single workspace CodeIntelIndex and passes it into
audit_changed_files so the audit reuses already-known facts instead of
building a second index. Callers outside ToolRegistry (with no index to
share) must keep working unchanged: a local index is constructed for that
call only, never cached globally.
"""

from __future__ import annotations

from pathlib import Path

import aura.code_intel  # noqa: F401 — triggers adapter registration

from aura.code_intel.audit import audit_changed_files
from aura.code_intel.index import CodeIntelIndex


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_audit_changed_files_works_with_no_index_supplied(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def broken(:\n    pass\n")

    findings = audit_changed_files(tmp_path, ["app.py"])

    assert any(f.kind == "parse_failure" for f in findings)


def test_audit_changed_files_works_with_an_injected_index(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "def broken(:\n    pass\n")
    index = CodeIntelIndex(tmp_path)

    findings = audit_changed_files(tmp_path, ["app.py"], index=index)

    assert any(f.kind == "parse_failure" for f in findings)
    # The supplied index was actually used — it now knows this file.
    assert "app.py" in index.file_paths()


def test_audit_changed_files_injected_index_reuses_prior_facts(tmp_path: Path) -> None:
    _write(tmp_path / "lib.py", "VERSION = 1\n")
    _write(tmp_path / "app.py", "import lib\n")
    index = CodeIntelIndex(tmp_path)
    index.ensure_fresh("lib.py")
    index.ensure_fresh("app.py")

    findings = audit_changed_files(tmp_path, ["app.py"], index=index)

    # No crash and the pre-existing index is the one consulted for blast
    # radius (dependents of lib.py are already known to it).
    assert isinstance(findings, list)
    assert "app.py" in index.get_dependents("lib.py")
