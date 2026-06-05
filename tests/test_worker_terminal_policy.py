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
        assert payload["error"] == (
            "Worker terminal supports validation/build commands and safe project-local dependency setup. "
            "Use structured read tools for source inspection."
        )
        assert payload["suggested_next_tool"] == "read_file"
        assert payload["suggested_next_action"] == (
            "Use read_file, read_files, grep_search, read_file_outline, find_usages, or "
            "search_codebase. If structured reads cannot access the file, report a blocker "
            "instead of trying terminal/Python file reads."
        )
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


def test_worker_terminal_policy_allows_project_local_dependency_setup(tmp_path) -> None:
    commands = [
        r".venv\Scripts\python.exe -m pip install -e .",
        r".venv\Scripts\python.exe -m pip install -e .[test]",
        r".venv\Scripts\python.exe -m pip install -e .[dev]",
        r".venv\Scripts\python.exe -m pip install -r requirements.txt",
        r".venv\Scripts\python.exe -m pip install -r requirements-dev.txt",
        ".venv/bin/python -m pip install -e .",
        ".venv/bin/python -m pip install -r requirements.txt",
    ]

    for command in commands:
        assert classify_worker_terminal_command(command) == "project_environment_setup"
        decision = worker_terminal_command_allowed(command, workspace_root=tmp_path)
        assert decision.allowed is True
        assert decision.failure_class == ""


def test_worker_terminal_policy_allows_project_manager_setup_with_project_evidence(tmp_path) -> None:
    commands_and_files = [
        ("uv sync", "pyproject.toml"),
        ("uv sync --all-extras", "uv.lock"),
        ("uv sync --dev", "pyproject.toml"),
        ("poetry install", "poetry.lock"),
        ("pdm install", "pdm.toml"),
    ]

    for command, filename in commands_and_files:
        workspace = tmp_path / command.replace(" ", "_").replace("-", "_")
        workspace.mkdir()
        (workspace / filename).write_text("", encoding="utf-8")

        assert classify_worker_terminal_command(command) == "project_environment_setup"
        decision = worker_terminal_command_allowed(command, workspace_root=workspace)

        assert decision.allowed is True
        assert decision.failure_class == ""


def test_worker_terminal_policy_blocks_project_manager_setup_without_project_evidence(tmp_path) -> None:
    commands = [
        "uv sync",
        "uv sync --all-extras",
        "uv sync --dev",
        "poetry install",
        "pdm install",
    ]

    for command in commands:
        decision = worker_terminal_command_allowed(command, workspace_root=tmp_path)
        payload = decision.to_blocked_payload(command)

        assert decision.allowed is False
        assert payload["failure_class"] == "project_environment_setup_blocked"


def test_worker_terminal_policy_allows_absolute_workspace_venv_python_pip_install(tmp_path) -> None:
    windows_command = (
        r"C:\workspaces\demo\.venv\Scripts\python.exe -m pip install -e ."
    )
    posix_python = tmp_path / ".venv" / "bin" / "python"
    posix_command = f"{posix_python} -m pip install -e ."

    assert worker_terminal_command_allowed(windows_command).allowed is True
    assert worker_terminal_command_allowed(posix_command, workspace_root=tmp_path).allowed is True


def test_worker_terminal_policy_allows_venv_creation_only_without_existing_project_venv(tmp_path) -> None:
    command = "python -m venv .venv"

    decision = worker_terminal_command_allowed(command, workspace_root=tmp_path)
    assert classify_worker_terminal_command(command) == "project_environment_setup"
    assert decision.allowed is True

    existing = tmp_path / ".venv" / "Scripts" / "python.exe"
    existing.parent.mkdir(parents=True)
    existing.write_text("", encoding="utf-8")

    blocked = worker_terminal_command_allowed(command, workspace_root=tmp_path)
    payload = blocked.to_blocked_payload(command)
    assert blocked.allowed is False
    assert payload["failure_class"] == "project_environment_setup_blocked"


def test_worker_terminal_policy_blocks_global_dependency_setup() -> None:
    commands = [
        "pip install fastapi",
        "pip3 install fastapi",
        "python -m pip install fastapi",
        "py -m pip install fastapi",
        "sudo apt install python3-fastapi",
        "apt install python3-fastapi",
        "winget install Python.Python.3.13",
        "choco install python",
        "brew install python",
        "npm install -g typescript",
    ]

    for command in commands:
        decision = worker_terminal_command_allowed(command)
        payload = decision.to_blocked_payload(command)
        assert decision.allowed is False
        assert payload["failure_class"] == "global_environment_setup_blocked"


def test_worker_terminal_policy_blocks_explicit_global_dependency_setup() -> None:
    command = "pip install fastapi"

    decision = worker_terminal_command_allowed(
        command,
        explicit_validation_commands=[command],
    )
    payload = decision.to_blocked_payload(command)

    assert decision.allowed is False
    assert payload["failure_class"] == "global_environment_setup_blocked"


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
