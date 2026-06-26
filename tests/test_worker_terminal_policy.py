from aura.conversation.terminal_policy import (
    classify_worker_terminal_command,
    worker_terminal_command_allowed,
)


def test_worker_terminal_policy_allows_common_coding_agent_commands() -> None:
    commands = [
        "python -m compileall yardline tests scripts",
        "python -m py_compile tests/test_api_depots.py",
        r".venv\Scripts\python.exe -m compileall yardline tests scripts",
        r".venv\Scripts\python.exe -m py_compile tests\test_api_depots.py",
        "pytest -q",
        "python -m pytest -q",
        "ruff check aura",
        "mypy aura",
        "cat graph_main_window.py",
        "type graph_main_window.py",
        "Get-Content graph_main_window.py",
        'rg "def main" aura',
        "grep -R main aura",
        "git status",
        "git diff",
        "git reset --soft HEAD~1",
        r".venv\Scripts\python.exe -m pip install -e .",
        r".venv\Scripts\python.exe -m pip install pytest httpx",
        "python -m pip install pytest httpx",
        "pip install fastapi",
        "uv sync",
        "poetry install",
        "pdm install",
        "npm install",
        "npm run build",
        "cargo test",
        "go test ./...",
        'python -c "import subprocess; subprocess.run([\'git\', \'status\'])"',
    ]

    for command in commands:
        assert classify_worker_terminal_command(command) == "terminal"
        decision = worker_terminal_command_allowed(command)
        assert decision.allowed is True
        assert decision.failure_class == ""
        assert decision.to_blocked_payload(command)["blocked_command"] == command


def test_worker_terminal_policy_classifies_empty_command() -> None:
    assert classify_worker_terminal_command("") == "empty"
    decision = worker_terminal_command_allowed("")
    assert decision.allowed is True


def test_worker_terminal_policy_blocks_python_source_inspection() -> None:
    command = 'python -c "from pathlib import Path; print(Path(\'graph_main_window.py\').read_text())"'

    decision = worker_terminal_command_allowed(command)

    assert decision.allowed is False
    assert decision.failure_class == "source_inspection_command_blocked"
    assert decision.suggested_next_tool == "read_file"
    assert decision.to_blocked_payload(command)["blocked_command"] == command


def test_worker_terminal_policy_allows_explicit_validation_command() -> None:
    command = 'python -c "from pathlib import Path; print(Path(\'graph_main_window.py\').read_text())"'

    decision = worker_terminal_command_allowed(
        command,
        explicit_validation_commands=[command],
    )

    assert decision.allowed is True
