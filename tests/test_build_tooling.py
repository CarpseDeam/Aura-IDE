"""Regression coverage for the Aura Windows build tooling.

Everything here runs against mocked subprocesses and temporary directories:
no Nuitka, no pip, no Inno Setup, no GitHub, no network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.aura_build import assets, cli, config, environment, release

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "scripts" / "build_nuitka.py"
STUB_PYTHON = Path("python.exe")


@pytest.fixture(autouse=True)
def _no_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generated commands independent of the developer's signing setup."""
    monkeypatch.delenv(config.SIGN_CERT_ENV, raising=False)
    monkeypatch.delenv(config.SIGN_PASS_ENV, raising=False)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A throwaway repo root with venv creation and pip installs stubbed out."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "aura"\nversion = "2.0.06"\n', encoding="utf-8")

    created: list[Path] = []
    installs: list[list[str]] = []

    def fake_create(path, **kwargs):
        exe = Path(path) / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("stub", encoding="utf-8")
        created.append(Path(path))

    monkeypatch.setattr(environment.venv, "create", fake_create)
    monkeypatch.setattr(environment, "run", lambda cmd, **kwargs: installs.append(list(cmd)))
    return SimpleNamespace(root=root, created=created, installs=installs)


def marker_path(root: Path) -> Path:
    return environment.build_venv_dir(root) / config.BUILD_VENV_MARKER_NAME


# ── Nuitka command defaults ──────────────────────────────────────────────────


def test_default_command_omits_low_memory_and_cache_clean() -> None:
    cmd = cli.create_nuitka_command(STUB_PYTHON)
    assert "--low-memory" not in cmd
    assert "--clean-cache=all" not in cmd


def test_clean_build_adds_cache_clean() -> None:
    cmd = cli.create_nuitka_command(STUB_PYTHON, clean=True)
    assert "--clean-cache=all" in cmd
    assert cmd.index("--clean-cache=all") < cmd.index("--assume-yes-for-downloads")


def test_low_memory_command_sets_low_memory_and_single_job() -> None:
    cmd = cli.create_nuitka_command(STUB_PYTHON, low_memory=True, jobs=1)
    assert "--low-memory" in cmd
    assert "--jobs=1" in cmd


def test_compilation_report_arguments_are_always_present() -> None:
    assert config.NUITKA_REPORT_PATH == "build/nuitka-compilation-report.xml"
    for kwargs in ({}, {"clean": True}, {"low_memory": True, "jobs": 1}, {"jobs": 3}):
        cmd = cli.create_nuitka_command(STUB_PYTHON, **kwargs)
        assert f"--report={config.NUITKA_REPORT_PATH}" in cmd
        assert "--report-diffable" in cmd


def test_packaged_feature_flags_are_preserved() -> None:
    cmd = cli.create_nuitka_command(STUB_PYTHON)
    for expected in (
        "--standalone",
        "--enable-plugin=pyside6",
        "--include-package=aura",
        "--include-package=relay",
        "--include-package=playwright",
        "--include-package-data=playwright",
        "--include-package=greenlet",
        "--include-package=pyee",
        "--nofollow-import-to=charset_normalizer",
        "--nofollow-import-to=click",
        "--nofollow-import-to=google",
        "--lto=no",
    ):
        assert expected in cmd
    assert cmd[-1] == config.PACKAGE_NAME


def test_signing_arguments_follow_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.signing_arguments() == []
    monkeypatch.setenv(config.SIGN_CERT_ENV, "cert.pfx")
    assert cli.signing_arguments() == ["--windows-sign-certificate=cert.pfx"]
    monkeypatch.setenv(config.SIGN_PASS_ENV, "hunter2")
    assert cli.signing_arguments() == [
        "--windows-sign-certificate=cert.pfx",
        "--windows-sign-certificate-password=hunter2",
    ]


# ── Job resolution ───────────────────────────────────────────────────────────


def test_default_effective_jobs_are_positive_and_capped_at_four() -> None:
    jobs = cli.resolve_effective_jobs(None, low_memory=False)
    assert jobs == config.DEFAULT_NUITKA_JOBS
    assert 1 <= jobs <= 4
    assert f"--jobs={jobs}" in cli.create_nuitka_command(STUB_PYTHON, jobs=jobs)


def test_explicit_jobs_are_honored() -> None:
    assert cli.resolve_effective_jobs(7, low_memory=False) == 7
    assert "--jobs=7" in cli.create_nuitka_command(STUB_PYTHON, jobs=7)


def test_low_memory_produces_a_single_effective_job() -> None:
    assert cli.resolve_effective_jobs(None, low_memory=True) == 1
    assert cli.resolve_effective_jobs(16, low_memory=True) == 1


@pytest.mark.parametrize("jobs", [0, -1, -8])
def test_invalid_job_counts_are_rejected(jobs: int) -> None:
    with pytest.raises(SystemExit, match="--jobs"):
        cli.resolve_effective_jobs(jobs, low_memory=False)
    with pytest.raises(SystemExit, match="--jobs"):
        cli.resolve_effective_jobs(jobs, low_memory=True)
    with pytest.raises(SystemExit, match="--jobs"):
        cli.create_nuitka_command(STUB_PYTHON, jobs=jobs)


def test_invalid_job_count_fails_before_any_build_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("build work must not start with an invalid job count")

    monkeypatch.setattr(cli.os, "chdir", explode)
    monkeypatch.setattr(cli, "validate_project_paths", explode)
    monkeypatch.setattr(cli, "prepare_build_venv", explode)
    monkeypatch.setattr(cli, "run", explode)

    with pytest.raises(SystemExit, match="--jobs"):
        cli.build(jobs=0, skip_version_update=True)


# ── Build venv reuse ─────────────────────────────────────────────────────────


def test_compatible_marker_reuses_the_existing_venv(fake_repo: SimpleNamespace) -> None:
    first = environment.prepare_build_venv(fake_repo.root)
    assert first.reused is False
    assert len(fake_repo.created) == 1
    assert len(fake_repo.installs) == 1

    second = environment.prepare_build_venv(fake_repo.root)
    assert second.reused is True
    assert second.python_exe == first.python_exe
    assert len(fake_repo.created) == 1
    assert len(fake_repo.installs) == 1


def test_source_changes_alone_do_not_refresh_the_venv(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    package_dir = fake_repo.root / "aura"
    package_dir.mkdir()
    (package_dir / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert environment.prepare_build_venv(fake_repo.root).reused is True
    assert len(fake_repo.installs) == 1


def test_missing_marker_refreshes_the_venv(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    marker_path(fake_repo.root).unlink()

    assert environment.prepare_build_venv(fake_repo.root).reused is False
    assert len(fake_repo.created) == 2


def test_corrupt_marker_refreshes_the_venv(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    marker_path(fake_repo.root).write_text("{not valid json", encoding="utf-8")

    assert environment.prepare_build_venv(fake_repo.root).reused is False
    assert len(fake_repo.created) == 2


@pytest.mark.parametrize(
    "key, value",
    [
        ("python_version", "3.0.0"),
        ("python_executable", "C:\\somewhere\\else\\python.exe"),
        ("python_architecture", "sparc-32bit"),
        ("schema", config.BUILD_VENV_MARKER_SCHEMA - 1),
        ("build_requirements", ["-e", "."]),
    ],
)
def test_incompatible_marker_fields_refresh_the_venv(
    fake_repo: SimpleNamespace, key: str, value: object
) -> None:
    environment.prepare_build_venv(fake_repo.root)
    path = marker_path(fake_repo.root)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored[key] = value
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert environment.prepare_build_venv(fake_repo.root).reused is False
    assert len(fake_repo.created) == 2


def test_pyproject_change_refreshes_the_venv(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    (fake_repo.root / "pyproject.toml").write_text(
        '[project]\nname = "aura"\nversion = "2.0.06"\ndependencies = ["httpx"]\n',
        encoding="utf-8",
    )

    assert environment.prepare_build_venv(fake_repo.root).reused is False
    assert len(fake_repo.created) == 2


def test_clean_refreshes_an_otherwise_compatible_venv(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    assert environment.prepare_build_venv(fake_repo.root, clean=True).reused is False
    assert len(fake_repo.created) == 2
    assert len(fake_repo.installs) == 2


def test_refreshed_venv_installs_the_declared_build_requirements(fake_repo: SimpleNamespace) -> None:
    environment.prepare_build_venv(fake_repo.root)
    install_cmd = fake_repo.installs[0]
    assert install_cmd[1:5] == ["-m", "pip", "--isolated", "install"]
    assert install_cmd[5:] == list(config.BUILD_REQUIREMENTS)


# ── Phase timing and reporting ───────────────────────────────────────────────


def test_phase_failure_is_reported_without_swallowing_it(capsys: pytest.CaptureFixture[str]) -> None:
    timeline = cli.BuildTimeline()
    with timeline.phase("first phase"):
        pass

    with pytest.raises(RuntimeError, match="boom"):
        with timeline.phase("second phase"):
            raise RuntimeError("boom")

    timeline.print_summary()
    out = capsys.readouterr().out

    assert [record.name for record in timeline.phases] == ["first phase", "second phase"]
    assert timeline.phases[0].failed is False
    assert timeline.phases[1].failed is True
    assert "first phase: starting" in out
    assert "second phase: FAILED" in out
    assert "Build summary" in out


def test_summary_reports_every_required_fact() -> None:
    timeline = cli.BuildTimeline(clean_build=True, low_memory=True, effective_jobs=1)
    timeline.venv_reused = False
    timeline.dist_bytes = 1024 * 1024
    timeline.installer_bytes = 2048
    with timeline.phase("only phase"):
        pass

    text = "\n".join(timeline.summary_lines())
    assert "total elapsed" in text
    assert "only phase" in text
    assert "refreshed" in text
    assert "clean" in text
    assert "low-memory mode forces a single job" in text
    assert "1.0 MiB" in text
    assert "2.0 KiB" in text


def test_build_reports_phases_when_compilation_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(cli.os, "chdir", lambda path: None)
    monkeypatch.setattr(cli, "validate_project_paths", lambda root: None)
    monkeypatch.setattr(cli, "validate_release_prerequisites", lambda installer, github: None)
    monkeypatch.setattr(cli, "resolve_build_version", lambda root, version, **kwargs: "2.0.06")
    monkeypatch.setattr(cli, "clean_previous_dist_dirs", lambda root: None)
    monkeypatch.setattr(
        cli,
        "prepare_build_venv",
        lambda root, clean=False: environment.BuildVenv(python_exe=STUB_PYTHON, reused=True),
    )

    def failing_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        raise subprocess.CalledProcessError(3, cmd)

    monkeypatch.setattr(cli, "run", failing_run)

    with pytest.raises(SystemExit, match="exit code 3"):
        cli.build(skip_version_update=True, installer=False)

    out = capsys.readouterr().out
    assert "Nuitka compilation: FAILED" in out
    assert "Build summary" in out
    assert "prerequisites & version resolution" in out
    assert "Build venv:        reused" in out

    # The default build really is warm, parallel, and reported.
    assert "--low-memory" not in captured["cmd"]
    assert "--clean-cache=all" not in captured["cmd"]
    assert f"--jobs={config.DEFAULT_NUITKA_JOBS}" in captured["cmd"]
    assert "--report-diffable" in captured["cmd"]


# ── Installer / release option validation ────────────────────────────────────


def test_github_release_requires_an_installer() -> None:
    with pytest.raises(SystemExit, match="requires an installer"):
        release.validate_release_prerequisites(False, True)


def test_installer_flag_requires_inno_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "find_iscc", lambda: None)
    with pytest.raises(SystemExit, match="iscc.exe not found"):
        release.validate_release_prerequisites(True, False)


def test_no_release_options_touches_no_external_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("no external tool lookup expected")

    monkeypatch.setattr(release.shutil, "which", explode)
    release.validate_release_prerequisites(None, False)


def test_no_installer_skips_installer_creation(tmp_path: Path) -> None:
    assert release.create_installer(tmp_path, tmp_path, "2.0.06", False) is None


def test_version_and_skip_version_update_conflict(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot be used together"):
        environment.resolve_build_version(tmp_path, "1.2.3", skip_version_update=True)


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3.4", "abc", ""])
def test_invalid_versions_are_rejected(bad: str) -> None:
    with pytest.raises(SystemExit, match="Invalid version"):
        environment.normalize_version(bad)


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_new_cache_and_memory_flags_parse() -> None:
    args = cli.parse_args(["--clean", "--low-memory", "--skip-version-update", "--no-installer"])
    assert args.clean is True
    assert args.low_memory is True
    assert args.jobs is None
    assert args.no_installer is True


@pytest.mark.parametrize("flag", ["--fast", "--refresh-build-venv", "--no-low-memory"])
def test_removed_flags_have_no_hidden_aliases(flag: str) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([flag])


def test_release_options_are_still_accepted() -> None:
    args = cli.parse_args(["--version", "2.0.06", "--installer", "--github-release", "--create-github-release"])
    assert args.version == "2.0.06"
    assert args.installer is True
    assert args.github_release is True
    assert args.create_github_release is True


# ── Entry point ──────────────────────────────────────────────────────────────


def test_entry_point_reuses_the_canonical_cli_module() -> None:
    spec = importlib.util.spec_from_file_location("_aura_build_entry_point", ENTRY_POINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main is cli.main


def test_entry_point_is_executable() -> None:
    result = subprocess.run(
        [sys.executable, str(ENTRY_POINT), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert "--clean" in result.stdout
    assert "--low-memory" in result.stdout
    assert "--fast" not in result.stdout
    assert "--refresh-build-venv" not in result.stdout


def test_production_build_modules_stay_small() -> None:
    package_dir = REPO_ROOT / "scripts" / "aura_build"
    for module in sorted(package_dir.glob("*.py")):
        line_count = len(module.read_text(encoding="utf-8").splitlines())
        assert line_count < 500, f"{module.name} has {line_count} lines"


# ── Playwright runtime packaging ─────────────────────────────────────────────


def test_build_tooling_never_installs_chromium() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "scripts" / "aura_build").glob("*.py")
    )
    assert "playwright install chromium" not in source
    assert "bundle_chromium" not in source


def test_stale_distribution_browser_payload_is_removed(tmp_path: Path) -> None:
    dist = tmp_path / "Aura.dist"
    stale_browser = dist / "ms-playwright" / "chromium-old" / "chrome.exe"
    stale_browser.parent.mkdir(parents=True)
    stale_browser.write_bytes(b"old browser")

    assets.remove_distribution_browser_payload(dist)

    assert not (dist / "ms-playwright").exists()


def test_playwright_validation_needs_driver_assets_but_no_browser_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "Aura.dist"
    driver = dist / "playwright" / "driver"
    package = driver / "package"
    (package / "lib").mkdir(parents=True)
    for path in (driver / "node.exe", package / "package.json", package / "cli.js"):
        path.write_bytes(b"present")
    subprocess_run = MagicMock(return_value=SimpleNamespace(stdout="sync_api: OK\ngreenlet: OK\npyee: OK\n"))
    monkeypatch.setattr(assets.subprocess, "run", subprocess_run)

    assets.validate_playwright_bundle(dist, STUB_PYTHON)

    assert not (dist / "ms-playwright").exists()
    command = subprocess_run.call_args.args[0]
    assert command[:2] == [str(STUB_PYTHON), "-c"]
    assert "playwright.sync_api" in command[2]
    assert "greenlet" in command[2]
    assert "pyee" in command[2]
    assert all("install" not in str(part) for part in command)
