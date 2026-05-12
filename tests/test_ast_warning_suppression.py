from __future__ import annotations

import warnings
from pathlib import Path

from aura.conversation.tools.fs_edit_structured import propose_edit_symbol
from aura.conversation.tools.fs_read import read_file_outline
from aura.repo_map import generate_repo_map


SOURCE_WITH_INVALID_ESCAPE = "pattern = '\\\\S+'\n\n\ndef target():\n    return pattern\n"


def test_internal_ast_scans_do_not_emit_invalid_escape_warnings(tmp_path: Path) -> None:
    source_file = tmp_path / "bad_escape.py"
    source_file.write_text(SOURCE_WITH_INVALID_ESCAPE, encoding="utf-8")

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always", SyntaxWarning)

        repo_map = generate_repo_map(tmp_path)
        outline = read_file_outline(tmp_path, source_file)
        edit = propose_edit_symbol(
            tmp_path,
            source_file,
            "function",
            "target",
            "def target():\n    return pattern.upper()\n",
        )

    assert "bad_escape.py" in repo_map
    assert outline["ok"] is True
    assert edit["ok"] is True
    assert not [
        warning
        for warning in records
        if "invalid escape sequence" in str(warning.message)
    ]
