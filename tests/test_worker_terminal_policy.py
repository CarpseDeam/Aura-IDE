from aura.conversation.terminal_policy import (
    classify_worker_terminal_command,
    worker_terminal_command_allowed,
)


def test_worker_terminal_policy_blocks_source_inspection_commands() -> None:
    commands = [
        'python -c "from pathlib import Path; print(Path(\'graph_main_window.py\').read_text())"',
        'python -c "print(open(\'graph_main_window.py\').read())"',
        'python -c "import linecache; print(linecache.getline(\'graph_main_window.py\', 1))"',
        "cat graph_main_window.py",
        "type graph_main_window.py",
        "Get-Content graph_main_window.py",
        "gc graph_main_window.py",
        "sed -n '1,80p' graph_main_window.py",
        "awk '{print}' graph_main_window.py",
        "head graph_main_window.py",
        "tail graph_main_window.py",
        'rg "_on_create_variations" graph_main_window.py',
        'grep "_on_create_variations" graph_main_window.py',
        'findstr "_on_create_variations" graph_main_window.py',
    ]

    for command in commands:
        assert classify_worker_terminal_command(command) == "source_inspection"
        decision = worker_terminal_command_allowed(command)
        payload = decision.to_blocked_payload(command)
        assert decision.allowed is False
        assert payload["failure_class"] == "source_inspection_command_blocked"
        assert payload["suggested_next_tool"] == "read_file"
        assert payload["blocked_command"] == command


def test_worker_terminal_policy_allows_validation_commands() -> None:
    commands = [
        "python -m py_compile graph_main_window.py",
        "pytest tests/test_x.py",
        "python -m pytest tests/test_x.py",
        "python -m unittest tests.test_x",
        "ruff check aura",
        "ruff format --check aura",
        "mypy aura",
        "npm test",
        "npm run test",
        "npm run build",
        "cargo test",
        "cargo build",
        "go test ./...",
    ]

    for command in commands:
        assert classify_worker_terminal_command(command) == "validation"
        decision = worker_terminal_command_allowed(command)
        assert decision.allowed is True
        assert decision.failure_class == ""


def test_worker_terminal_policy_allows_explicit_validation_command() -> None:
    command = "python tools/custom_validation.py --smoke"

    decision = worker_terminal_command_allowed(
        command,
        explicit_validation_commands=[command],
    )

    assert classify_worker_terminal_command(command) == "unknown"
    assert decision.allowed is True
    assert decision.failure_class == ""


def test_worker_terminal_policy_blocks_unknown_commands_by_default() -> None:
    command = "python tools/custom_validation.py --smoke"
    decision = worker_terminal_command_allowed(command)
    payload = decision.to_blocked_payload(command)

    assert decision.allowed is False
    assert payload["failure_class"] == "worker_terminal_not_validation"


def test_worker_terminal_policy_allows_dependency_install_commands(tmp_path) -> None:
    commands = [
        r".venv\Scripts\python.exe -m pip install -e .",
        r".venv\Scripts\python.exe -m pip install pytest httpx",
        r".venv\Scripts\python.exe -m pip install -r requirements.txt",
        ".venv/bin/python -m pip install -e .",
        ".venv/bin/python -m pip install pytest httpx",
        "python -m pip install -e .",
        "python -m pip install pytest httpx",
        "pip install fastapi",
        "pip3 install fastapi",
        "uv sync",
        "uv sync --all-extras",
        "uv sync --dev",
        "uv pip install pytest",
        "poetry install",
        "pdm install",
        "npm install",
        "npm ci",
        "pnpm install",
        "yarn install",
        "cargo fetch",
        "go mod download",
        "go get ./...",
    ]

    for command in commands:
        assert classify_worker_terminal_command(command) == "dependency_install"
        decision = worker_terminal_command_allowed(command, workspace_root=tmp_path)
        assert decision.allowed is True
        assert decision.failure_class == ""


def test_worker_terminal_policy_allows_install_then_validation_chain(tmp_path) -> None:
    command = r".venv\Scripts\python.exe -m pip install pytest && python -m pytest"

    decision = worker_terminal_command_allowed(command, workspace_root=tmp_path)

    assert classify_worker_terminal_command(command) == "dependency_install"
    assert decision.allowed is True
    assert decision.failure_class == ""


def test_worker_terminal_policy_blocks_generic_git_reset() -> None:
    command = "git reset --soft HEAD~1"
    decision = worker_terminal_command_allowed(command)
    payload = decision.to_blocked_payload(command)

    assert classify_worker_terminal_command(command) == "unknown"
    assert decision.allowed is False
    assert payload["failure_class"] == "worker_terminal_not_validation"


def test_worker_terminal_policy_blocks_subprocess_bypass() -> None:
    command = 'python -c "import subprocess; subprocess.run([\'git\', \'status\'])"'
    decision = worker_terminal_command_allowed(command)
    payload = decision.to_blocked_payload(command)

    assert classify_worker_terminal_command(command) == "unknown"
    assert decision.allowed is False
    assert payload["failure_class"] == "worker_terminal_not_validation"
