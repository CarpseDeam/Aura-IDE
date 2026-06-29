from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools.diagnostic_handler import run_diagnostic_command


def test_run_diagnostic_command_uses_workspace_relative_cwd(tmp_path: Path) -> None:
    subdir = tmp_path / "companion-web"
    subdir.mkdir()
    proc = MagicMock(returncode=0, stdout="ok\n", stderr="")

    with (
        patch("aura.project_env.shutil_which", return_value="python"),
        patch("aura.conversation.tools.diagnostic_handler.subprocess.run", return_value=proc) as run,
    ):
        result = run_diagnostic_command(
            "python -m py_compile app.py",
            workspace_root=tmp_path,
            cwd="companion-web",
        )

    assert result["ok"] is True
    assert result["cwd"] == "companion-web"
    assert run.call_args.kwargs["cwd"] == subdir.resolve()


def test_run_diagnostic_command_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cwd must not escape"):
        run_diagnostic_command(
            "python -m py_compile app.py",
            workspace_root=tmp_path,
            cwd="../outside",
        )
