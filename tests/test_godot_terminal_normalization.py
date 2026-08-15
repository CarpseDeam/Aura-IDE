from __future__ import annotations

import threading
from pathlib import Path

from aura.conversation.history import History
from aura.conversation.tool_runner import ToolRunner
from aura.shell.powershell_session import PowerShellCommandResult


def test_execution_runs_import_after_validate_project_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import aura.conversation.command_normalizer as command_normalizer_module

    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    executable = tmp_path / "Godot Tools" / "Godot_v4.6.3-stable_win64.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    executed: list[str] = []
    rewrite_calls: list[str] = []
    real_rewrite = command_normalizer_module.build_project_command_rewrite

    def counted_rewrite(workspace_root: Path, command: str):
        rewrite_calls.append(command)
        return real_rewrite(workspace_root, command)

    monkeypatch.setattr(command_normalizer_module, "build_project_command_rewrite", counted_rewrite)

    class FakePowerShellSession:
        def __init__(self, workspace_root: Path) -> None:
            self.current_cwd = workspace_root
            self.session_id = "fake-session"

        def execute(self, command: str, *, on_output, on_submitted, **_kwargs) -> PowerShellCommandResult:
            executed.append(command)
            on_submitted()
            on_output("Godot import complete")
            return PowerShellCommandResult(
                ok=True,
                stdout="Godot import complete",
                stderr="",
                output="Godot import complete",
                exit_code=0,
                cwd=str(self.current_cwd),
                session_id=self.session_id,
                submitted=True,
                session_started=True,
            )

        def close(self) -> None:
            pass

    events = []
    runner = ToolRunner(History(), tmp_path, session_factory=FakePowerShellSession)
    requested = (
        f'"{executable}" --headless --path "{tmp_path}" --validate-project'
    )

    result = runner.handle_terminal_command(
        "godot-validation",
        {"command": requested},
        events.append,
        threading.Event(),
        explicit_validation_commands=None,
    )

    assert result is not None
    assert rewrite_calls == [requested]
    assert len(executed) == 1
    assert "--validate-project" not in executed[0]
    assert executed[0].endswith("--import")
    payload = result["_terminal_payload"]
    assert payload["command"] == executed[0]
    assert payload["normalized_command"] == executed[0]
    assert payload["prepared_command"] == executed[0]
    assert payload["executed_command"] == executed[0]
    assert payload["requested_command"] == requested
    assert payload["original_command"] == requested
    assert payload["validation_classification"] == "passed"
    assert payload["normalization_reason"].endswith("rewritten to --import")
