from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from aura.conversation.history import History
from aura.conversation.tool_runner import ToolRunner
from aura.sandbox import SandboxResult


def test_worker_executes_import_after_validate_project_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import aura.conversation.tool_runner as tool_runner_module

    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    executable = tmp_path / "Godot Tools" / "Godot_v4.6.3-stable_win64.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    executed: list[str] = []

    class FakeSandboxExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_terminal_command(self, *, command, **_kwargs) -> SandboxResult:
            executed.append(command)
            return SandboxResult(ok=True, stdout="Godot import complete", stderr="", exit_code=0)

    monkeypatch.setattr(tool_runner_module, "SandboxExecutor", FakeSandboxExecutor)
    monkeypatch.setattr(
        tool_runner_module,
        "load_settings",
        lambda: SimpleNamespace(sandbox_mode="host"),
    )
    events = []
    runner = ToolRunner(History(), tmp_path)
    requested = (
        f'"{executable}" --headless --path "{tmp_path}" --validate-project'
    )

    result = runner.handle_terminal_command(
        "godot-validation",
        {"command": requested},
        events.append,
        threading.Event(),
        "worker",
        explicit_validation_commands=None,
    )

    assert result is not None
    assert len(executed) == 1
    assert "--validate-project" not in executed[0]
    assert executed[0].endswith("--import")
    payload = result["_terminal_payload"]
    assert payload["command"] == executed[0]
    assert payload["validation_classification"] == "passed"
    assert payload["normalization_reason"].endswith("rewritten to --import")
