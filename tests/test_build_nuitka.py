"""Tests for the Nuitka build helper."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.build_nuitka import (
    DEFAULT_NUITKA_JOBS,
    FINAL_DIST_NAME,
    OUTPUT_DIR,
    PACKAGE_NAME,
    REQUIRED_MEDIA_FILES,
    UPDATER_HELPER_DIST_NAME,
    UPDATER_HELPER_SOURCE,
    create_nuitka_command,
    grammar_prewarm_script,
    normalize_version,
    parse_args,
    prewarm_grammars,
    read_current_version,
    validate_project_paths,
    zip_distribution,
)


def test_normalize_version_accepts_optional_v_prefix() -> None:
    assert normalize_version("v1.3.4") == "1.3.4"
    assert normalize_version("1.3.4") == "1.3.4"


@pytest.mark.parametrize("version", ["", "1.3", "1.3.4.5", "1.3.beta"])
def test_normalize_version_rejects_invalid_values(version: str) -> None:
    with pytest.raises(SystemExit, match="Invalid version"):
        normalize_version(version)


def test_read_current_version_extracts_string_literal(tmp_path: Path) -> None:
    version_file = tmp_path / "aura" / "version.py"
    version_file.parent.mkdir()
    version_file.write_text(
        '"""Version information."""\n__version__ = "1.3.4"\n',
        encoding="utf-8",
    )

    assert read_current_version(tmp_path) == "1.3.4"


def test_parse_args_supports_noninteractive_flags() -> None:
    args = parse_args(["--skip-version-update", "--no-copy-desktop", "--jobs", "2", "--no-low-memory"])

    assert args.skip_version_update is True
    assert args.no_copy_desktop is True
    assert args.jobs == 2
    assert args.no_low_memory is True


def test_parse_args_defaults_to_low_memory_single_job() -> None:
    args = parse_args([])

    assert args.jobs == DEFAULT_NUITKA_JOBS
    assert args.no_low_memory is False


def test_create_nuitka_command_defaults_to_low_memory_single_job() -> None:
    cmd = create_nuitka_command()

    assert "--low-memory" in cmd
    assert f"--jobs={DEFAULT_NUITKA_JOBS}" in cmd
    assert cmd[-1] == PACKAGE_NAME


def test_create_nuitka_command_can_disable_low_memory() -> None:
    cmd = create_nuitka_command(low_memory=False, jobs=3)

    assert "--low-memory" not in cmd
    assert "--jobs=3" in cmd


def test_create_nuitka_command_rejects_zero_jobs() -> None:
    with pytest.raises(SystemExit, match="--jobs cannot be 0"):
        create_nuitka_command(jobs=0)


def test_grammar_prewarm_script_is_valid_python() -> None:
    compile(grammar_prewarm_script(), "<grammar-prewarm>", "exec")


def test_prewarm_grammars_invokes_python_snippet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def fake_check_output(cmd: list[str], stderr: int) -> bytes:
        calls.append(SimpleNamespace(cmd=cmd, stderr=stderr))
        grammar_dir = Path(cmd[3])
        grammar_dir.mkdir(parents=True, exist_ok=True)
        (grammar_dir / "javascript.so").write_bytes(b"grammar")
        return b"Downloaded languages: ['javascript']"

    monkeypatch.setattr("scripts.build_nuitka.subprocess.check_output", fake_check_output)

    prewarm_grammars(tmp_path / "Aura.dist", Path("python"))

    assert calls
    assert calls[0].cmd[0:2] == ["python", "-c"]
    assert calls[0].cmd[2] == grammar_prewarm_script()


def test_validate_project_paths_requires_all_media_files(tmp_path: Path) -> None:
    (tmp_path / "aura").mkdir()
    (tmp_path / "aura" / "__main__.py").write_text("", encoding="utf-8")
    (tmp_path / UPDATER_HELPER_SOURCE).write_text("helper", encoding="utf-8")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    for filename in REQUIRED_MEDIA_FILES:
        if filename != "working.png":
            (media_dir / filename).write_text("media", encoding="utf-8")

    with pytest.raises(SystemExit, match="working.png"):
        validate_project_paths(tmp_path)


def test_validate_project_paths_requires_updater_helper(tmp_path: Path) -> None:
    (tmp_path / "aura").mkdir()
    (tmp_path / "aura" / "__main__.py").write_text("", encoding="utf-8")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    for filename in REQUIRED_MEDIA_FILES:
        (media_dir / filename).write_text("media", encoding="utf-8")

    with pytest.raises(SystemExit, match="windows_updater.cmd"):
        validate_project_paths(tmp_path)


def test_validate_project_paths_accepts_complete_media_set(tmp_path: Path) -> None:
    (tmp_path / "aura").mkdir()
    (tmp_path / "aura" / "__main__.py").write_text("", encoding="utf-8")
    (tmp_path / UPDATER_HELPER_SOURCE).write_text("helper", encoding="utf-8")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    for filename in REQUIRED_MEDIA_FILES:
        (media_dir / filename).write_text("media", encoding="utf-8")

    validate_project_paths(tmp_path)


def test_zip_distribution_flattens_dist_contents(tmp_path: Path) -> None:
    final_dist_dir = tmp_path / OUTPUT_DIR / FINAL_DIST_NAME
    media_dir = final_dist_dir / "media"
    media_dir.mkdir(parents=True)
    (final_dist_dir / "Aura.exe").write_text("exe", encoding="utf-8")
    (final_dist_dir / UPDATER_HELPER_DIST_NAME).write_text("helper", encoding="utf-8")
    (media_dir / "test.txt").write_text("media", encoding="utf-8")
    (final_dist_dir / "runtime.dll").write_text("dll", encoding="utf-8")

    zip_distribution(tmp_path, final_dist_dir)

    zip_path = tmp_path / OUTPUT_DIR / "Aura-Windows-x64.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())

    assert "Aura.exe" in names
    assert UPDATER_HELPER_DIST_NAME in names
    assert "media/test.txt" in names
    assert "runtime.dll" in names
    assert "Aura.dist/Aura.exe" not in names
