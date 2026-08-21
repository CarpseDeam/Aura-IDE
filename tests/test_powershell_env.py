from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aura.shell.powershell_session import (
    PowerShellSession,
    build_child_environment,
    resolve_powershell,
)


@pytest.fixture
def powershell_available() -> None:
    try:
        resolve_powershell()
    except FileNotFoundError:
        pytest.skip("PowerShell is not installed")


def _make_fake_venv(root: Path, name: str = ".venv") -> tuple[Path, Path]:
    """Create a fake ``<root>/<name>/Scripts/python.exe`` and return (scripts_dir, venv_root)."""
    scripts = root / name / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    return scripts, scripts.parent


def _path_entries(env: dict[str, str]) -> list[str]:
    return [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]


# 1. Parent Aura venv + different workspace .venv -> child env has only the
#    workspace venv active.
def test_workspace_venv_replaces_inherited_aura_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_scripts, workspace_venv_root = _make_fake_venv(workspace)

    monkeypatch.setenv("VIRTUAL_ENV", str(aura_venv_root))
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "(aura-runtime)")
    monkeypatch.setenv("PATH", os.pathsep.join([str(aura_scripts), r"C:\Windows\System32"]))

    env = build_child_environment(workspace)

    assert env["VIRTUAL_ENV"] == str(workspace_venv_root)
    assert "VIRTUAL_ENV_PROMPT" not in env
    entries = _path_entries(env)
    assert entries.count(str(workspace_scripts)) == 1
    assert str(aura_scripts) not in entries
    assert r"C:\Windows\System32" in entries


# 2. Workspace without a venv does not inherit Aura's venv, while unrelated
#    PATH entries and environment variables are preserved.
def test_workspace_without_venv_does_not_inherit_aura_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace-plain"
    workspace.mkdir()

    monkeypatch.setenv("VIRTUAL_ENV", str(aura_venv_root))
    monkeypatch.setenv("PYTHONHOME", str(aura_venv_root))
    monkeypatch.setenv("MY_UNRELATED_TOKEN", "keep-me")
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(aura_scripts), r"C:\Windows\System32", r"C:\Tools"])
    )

    env = build_child_environment(workspace)

    assert "VIRTUAL_ENV" not in env
    assert "PYTHONHOME" not in env
    assert env["MY_UNRELATED_TOKEN"] == "keep-me"
    entries = _path_entries(env)
    assert str(aura_scripts) not in entries
    assert r"C:\Windows\System32" in entries
    assert r"C:\Tools" in entries


# 3. PATH insertion is not duplicated, even across repeated computation or
#    when the entry is already present.
def test_path_insertion_is_not_duplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_scripts, _ = _make_fake_venv(workspace)

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(workspace_scripts), r"C:\Windows\System32"]))

    env = build_child_environment(workspace)
    entries = _path_entries(env)
    assert entries.count(str(workspace_scripts)) == 1
    assert entries[0] == str(workspace_scripts)

    env_again = build_child_environment(workspace)
    assert _path_entries(env_again).count(str(workspace_scripts)) == 1


def test_path_removal_tolerates_quotes_and_trailing_separators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("VIRTUAL_ENV", str(aura_venv_root))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([f'"{aura_scripts}"', f"{aura_scripts}\\", r"C:\Windows\System32"]),
    )

    env = build_child_environment(workspace)
    assert _path_entries(env) == [r"C:\Windows\System32"]


# Aura's runtime venv is detected via sys.prefix even when VIRTUAL_ENV is
# absent from the inherited environment (e.g. Aura launched directly through
# a venv interpreter).
def test_runtime_venv_detected_via_sys_prefix_without_virtual_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura.shell import powershell_session

    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace-plain"
    workspace.mkdir()

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV_PROMPT", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("MY_UNRELATED_TOKEN", "keep-me")
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(aura_scripts), r"C:\Windows\System32"])
    )
    monkeypatch.setattr(powershell_session.sys, "prefix", str(aura_venv_root))
    monkeypatch.setattr(powershell_session.sys, "base_prefix", str(tmp_path / "system-python"))

    env = powershell_session.build_child_environment(workspace)

    assert "VIRTUAL_ENV" not in env
    assert env["MY_UNRELATED_TOKEN"] == "keep-me"
    entries = _path_entries(env)
    assert str(aura_scripts) not in entries
    assert r"C:\Windows\System32" in entries


# Duplicate runtime roots (VIRTUAL_ENV and sys.prefix pointing at the same
# directory) are deduplicated rather than processed twice.
def test_runtime_roots_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura.shell import powershell_session

    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace-plain"
    workspace.mkdir()

    monkeypatch.setenv("VIRTUAL_ENV", str(aura_venv_root))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(aura_scripts), r"C:\Windows\System32"])
    )
    monkeypatch.setattr(powershell_session.sys, "prefix", str(aura_venv_root))
    monkeypatch.setattr(powershell_session.sys, "base_prefix", str(tmp_path / "system-python"))

    env = powershell_session.build_child_environment(workspace)

    entries = _path_entries(env)
    assert str(aura_scripts) not in entries
    assert r"C:\Windows\System32" in entries


# When sys.prefix equals sys.base_prefix (ordinary system Python, no venv
# active), that path must not be treated as a runtime root to strip.
def test_sys_prefix_equal_to_base_prefix_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura.shell import powershell_session

    system_python_dir = tmp_path / "system-python"
    system_scripts = system_python_dir / "Scripts"
    system_scripts.mkdir(parents=True)
    workspace = tmp_path / "workspace-plain"
    workspace.mkdir()

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(system_scripts), r"C:\Windows\System32"])
    )
    monkeypatch.setattr(powershell_session.sys, "prefix", str(system_python_dir))
    monkeypatch.setattr(powershell_session.sys, "base_prefix", str(system_python_dir))

    env = powershell_session.build_child_environment(workspace)

    entries = _path_entries(env)
    assert str(system_scripts) in entries
    assert r"C:\Windows\System32" in entries


# 4. The environment is recomputed after session restart and per workspace.
def test_environment_recomputed_after_restart(
    tmp_path: Path, powershell_available: None
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = PowerShellSession(workspace)
    try:
        before = session.execute('Write-Output "[$env:VIRTUAL_ENV]"', timeout=10)
        session.close()
        _, workspace_venv_root = _make_fake_venv(workspace)
        after = session.execute('Write-Output "[$env:VIRTUAL_ENV]"', timeout=10)
    finally:
        session.close()

    assert before.ok and after.ok
    assert "[]" in before.output
    assert f"[{workspace_venv_root}]" in after.output
    assert before.session_id != after.session_id


def test_environment_differs_per_workspace_session(
    tmp_path: Path, powershell_available: None
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    workspace_b = tmp_path / "workspace-b"
    _, venv_b_root = _make_fake_venv(workspace_b)

    session_a = PowerShellSession(workspace_a)
    session_b = PowerShellSession(workspace_b)
    try:
        result_a = session_a.execute('Write-Output "[$env:VIRTUAL_ENV]"', timeout=10)
        result_b = session_b.execute('Write-Output "[$env:VIRTUAL_ENV]"', timeout=10)
    finally:
        session_a.close()
        session_b.close()

    assert "[]" in result_a.output
    assert f"[{venv_b_root}]" in result_b.output


# 5. Original failure path: a multiline command using bare `python` resolves
#    to the workspace interpreter, not an inherited Aura interpreter.
def test_multiline_bare_python_resolves_to_workspace_interpreter(
    tmp_path: Path, powershell_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    aura_scripts, aura_venv_root = _make_fake_venv(tmp_path / "aura-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_scripts, _ = _make_fake_venv(workspace)

    monkeypatch.setenv("VIRTUAL_ENV", str(aura_venv_root))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(aura_scripts), os.environ.get("PATH", "")])
    )

    session = PowerShellSession(workspace)
    try:
        multiline_command = (
            "$resolved = (Get-Command python).Source\n"
            'Write-Output "RESOLVED=$resolved"\n'
        )
        result = session.execute(multiline_command, timeout=10)
    finally:
        session.close()

    assert result.ok
    assert f"RESOLVED={workspace_scripts / 'python.exe'}" in result.output
    assert str(aura_scripts) not in result.output
