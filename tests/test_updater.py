"""Tests for Aura's source-install git updater."""

from __future__ import annotations

from pathlib import Path

import pytest

import aura.updater as updater
from aura.updater import (
    GitCommandResult,
    GitHubAsset,
    GitHubRelease,
    download_packaged_installer,
    find_extracted_app_root,
    get_app_repo_root,
    get_update_status,
    install_packaged_update,
    launch_downloaded_installer,
    pull_latest,
)


def _installer_release(version: str = "1.4.6") -> GitHubRelease:
    return GitHubRelease(
        tag=f"v{version}",
        version=version,
        assets=[GitHubAsset(name=f"AuraSetup-{version}.exe", url="https://example.invalid/installer.exe", size=123)],
        html_url="https://example.invalid/release",
    )


def test_find_extracted_app_root_uses_flattened_zip_root(tmp_path: Path) -> None:
    (tmp_path / "Aura.exe").write_text("exe", encoding="utf-8")
    (tmp_path / "media").mkdir()

    assert find_extracted_app_root(tmp_path) == tmp_path


def test_find_extracted_app_root_uses_legacy_dist_root(tmp_path: Path) -> None:
    legacy_root = tmp_path / "Aura.dist"
    legacy_root.mkdir()
    (legacy_root / "Aura.exe").write_text("exe", encoding="utf-8")
    (legacy_root / "media").mkdir()

    assert find_extracted_app_root(tmp_path) == legacy_root


def test_find_extracted_app_root_reports_unsupported_layout(tmp_path: Path) -> None:
    (tmp_path / "README.txt").write_text("not an app", encoding="utf-8")
    (tmp_path / "payload").mkdir()

    with pytest.raises(RuntimeError) as exc_info:
        find_extracted_app_root(tmp_path)

    message = str(exc_info.value)
    assert "Aura.exe at archive root or Aura.dist/Aura.exe" in message
    assert "README.txt" in message
    assert "payload" in message


def test_get_app_repo_root_walks_up_from_package(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "aura"
    package.mkdir(parents=True)
    (repo / ".git").mkdir()
    module_file = package / "updater.py"
    module_file.write_text("# test", encoding="utf-8")

    monkeypatch.setattr(updater, "__file__", str(module_file))

    assert get_app_repo_root() == repo


def test_get_app_repo_root_returns_none_without_git(monkeypatch, tmp_path: Path) -> None:
    package = tmp_path / "install" / "aura"
    package.mkdir(parents=True)
    module_file = package / "updater.py"
    module_file.write_text("# test", encoding="utf-8")

    monkeypatch.setattr(updater, "__file__", str(module_file))

    assert get_app_repo_root() is None


def test_build_windows_updater_command_uses_argv_list(tmp_path: Path) -> None:
    updater_exe = tmp_path / "AuraUpdater.cmd"
    extracted_dir = tmp_path / "extracted"
    install_dir = tmp_path / "Aura.dist"
    current_exe = install_dir / "Aura.exe"

    cmd = updater._build_windows_updater_command(
        updater_exe,
        extracted_dir,
        install_dir,
        current_exe,
        1234,
    )

    assert cmd == [
        str(updater_exe),
        "--source",
        str(extracted_dir),
        "--target",
        str(install_dir),
        "--pid",
        "1234",
        "--restart",
        str(current_exe),
    ]


def test_launch_windows_updater_validates_and_logs_argv(monkeypatch, tmp_path: Path) -> None:
    updater_exe = tmp_path / "AuraUpdater.cmd"
    extracted_dir = tmp_path / "extracted"
    install_dir = tmp_path / "Aura.dist"
    updater_exe.write_text("@echo off\n", encoding="utf-8")
    extracted_dir.mkdir()
    install_dir.mkdir()
    argv = updater._build_windows_updater_command(
        updater_exe,
        extracted_dir,
        install_dir,
        install_dir / "Aura.exe",
        1234,
    )
    calls: list[tuple[list[str], dict[str, object]]] = []
    output: list[str] = []

    class FakeProcess:
        pass

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProcess:
        calls.append((cmd, kwargs))
        return FakeProcess()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    updater._launch_windows_updater(
        updater_exe=updater_exe,
        argv=argv,
        extracted_dir=extracted_dir,
        install_dir=install_dir,
        output_callback=output.append,
    )

    assert calls == [
        (
            argv,
            {
                "cwd": str(updater_exe.parent),
                "stdin": updater.subprocess.DEVNULL,
                "stdout": updater.subprocess.DEVNULL,
                "stderr": updater.subprocess.DEVNULL,
                "close_fds": True,
            },
        )
    ]
    assert f"Updater executable: {updater_exe}" in output
    assert f"Updater argv: {argv!r}" in output


def test_launch_windows_updater_rejects_missing_extracted_dir(tmp_path: Path) -> None:
    updater_exe = tmp_path / "AuraUpdater.cmd"
    install_dir = tmp_path / "Aura.dist"
    updater_exe.write_text("@echo off\n", encoding="utf-8")
    install_dir.mkdir()
    extracted_dir = tmp_path / "missing"
    argv = updater._build_windows_updater_command(
        updater_exe,
        extracted_dir,
        install_dir,
        install_dir / "Aura.exe",
        1234,
    )

    with pytest.raises(FileNotFoundError, match="Extracted update directory"):
        updater._launch_windows_updater(
            updater_exe=updater_exe,
            argv=argv,
            extracted_dir=extracted_dir,
            install_dir=install_dir,
        )


def test_installer_update_stages_in_localappdata_update_folder(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    release = _installer_release()
    downloads: list[tuple[Path, str | None]] = []

    def fake_download_asset(
        asset: GitHubAsset,
        temp_dir: Path,
        output_callback=None,
        *,
        filename: str | None = None,
    ) -> Path:
        downloads.append((temp_dir, filename))
        path = temp_dir / (filename or asset.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installer")
        return path

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(updater, "_download_asset", fake_download_asset)
    monkeypatch.setattr(updater, "_launch_installer", lambda installer_path, output_callback=None: True)

    result = install_packaged_update(release)

    expected_dir = local_app_data / "Aura" / "updates" / "1.4.6"
    assert result.success is True
    assert downloads == [(expected_dir, "AuraSetup-1.4.6.exe")]
    assert (expected_dir / "AuraSetup-1.4.6.exe").exists()


def test_download_packaged_installer_returns_launch_pending_without_launch(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    release = _installer_release()

    def fake_download_asset(
        asset: GitHubAsset,
        temp_dir: Path,
        output_callback=None,
        *,
        filename: str | None = None,
    ) -> Path:
        path = temp_dir / (filename or asset.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installer")
        return path

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(updater, "_download_asset", fake_download_asset)
    monkeypatch.setattr(updater, "_launch_installer", lambda *args, **kwargs: pytest.fail("launch should be deferred"))

    result = download_packaged_installer(release)

    installer_path = local_app_data / "Aura" / "updates" / "1.4.6" / "AuraSetup-1.4.6.exe"
    assert result.success is True
    assert result.launch_pending is True
    assert result.target_version == "1.4.6"
    assert result.installer_path == installer_path


def test_installer_shell_execute_failure_returns_pull_result_false_with_path(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    release = _installer_release()
    output: list[str] = []

    def fake_download_asset(
        asset: GitHubAsset,
        temp_dir: Path,
        output_callback=None,
        *,
        filename: str | None = None,
    ) -> Path:
        path = temp_dir / (filename or asset.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installer")
        return path

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(updater, "_download_asset", fake_download_asset)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "_shellexecute_installer", lambda installer_path, flags: 31)

    result = install_packaged_update(release, output_callback=output.append)

    installer_path = local_app_data / "Aura" / "updates" / "1.4.6" / "AuraSetup-1.4.6.exe"
    assert result.success is False
    assert str(installer_path) in result.message
    assert "Launch method: ShellExecuteW/open" in result.message
    assert "If the installer does not appear, run this file manually" in result.message
    assert f"Downloaded installer: {installer_path}" in output
    assert "Installer launch failed: ShellExecuteW returned 31" in output


def test_launch_downloaded_installer_failure_returns_pull_result_false(monkeypatch, tmp_path: Path) -> None:
    installer_path = tmp_path / "AuraSetup-1.4.6.exe"
    installer_path.write_bytes(b"installer")
    output: list[str] = []

    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "_shellexecute_installer", lambda installer_path, flags: 31)

    result = launch_downloaded_installer(installer_path, output_callback=output.append, target_version="1.4.6")

    assert result.success is False
    assert result.target_version == "1.4.6"
    assert result.installer_path == installer_path
    assert str(installer_path) in result.message
    assert "Aura will now exit" not in output


def test_installer_shell_execute_success_returns_pull_result_true(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    release = _installer_release()
    launched: list[tuple[Path, list[str]]] = []
    output: list[str] = []

    def fake_download_asset(
        asset: GitHubAsset,
        temp_dir: Path,
        output_callback=None,
        *,
        filename: str | None = None,
    ) -> Path:
        path = temp_dir / (filename or asset.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installer")
        return path

    def fake_shellexecute(installer_path: Path, flags: list[str]) -> int:
        launched.append((installer_path, flags))
        return 33

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(updater, "_download_asset", fake_download_asset)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "_shellexecute_installer", fake_shellexecute)

    result = install_packaged_update(release, output_callback=output.append)

    installer_path = local_app_data / "Aura" / "updates" / "1.4.6" / "AuraSetup-1.4.6.exe"
    assert result.success is True
    assert launched == [(installer_path, ["/CURRENTUSER", "/LAUNCHAFTERUPDATE=1"])]
    assert "Launch method: ShellExecuteW/open" in output


def test_missing_installer_launch_gives_manual_recovery_instructions(monkeypatch, tmp_path: Path) -> None:
    output: list[str] = []
    installer_path = tmp_path / "missing" / "AuraSetup-1.4.6.exe"

    monkeypatch.setattr(updater.sys, "platform", "win32")

    launched = updater._launch_installer(installer_path, output_callback=output.append)

    assert launched is False
    assert f"Installer file is missing: {installer_path}" in output
    assert f"If the installer does not appear, run this file manually: {installer_path}" in output


def test_get_update_status_reports_not_git(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_app_repo_root", lambda: None)

    status = get_update_status()

    assert status.is_git_repo is False
    assert status.state == "not_git"
    assert "source installs" in status.message


def test_get_update_status_reports_no_upstream(monkeypatch, tmp_path: Path) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 128, "", "fatal: no upstream")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "no_upstream"
    assert status.branch == "main"
    assert status.commit == "abc1234"
    assert status.upstream is None
    assert status.can_pull is False


def test_get_update_status_classifies_behind_and_clean(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "fetch output\n", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "0\t2\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "behind"
    assert status.ahead == 0
    assert status.behind == 2
    assert status.has_local_changes is False
    assert status.can_pull is True


def test_get_update_status_disables_pull_with_local_changes(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, " M aura/updater.py\n", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "0 1\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "behind"
    assert status.has_local_changes is True
    assert status.can_pull is False
    assert "Commit, stash, or discard" in status.message


def test_get_update_status_fails_closed_when_status_fails(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 128, "", "fatal: status failed")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "error"
    assert status.can_pull is False
    assert "local changes" in status.message


def test_get_update_status_classifies_diverged(monkeypatch, tmp_path: Path) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "1 2\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "diverged"
    assert status.can_pull is False


def test_pull_latest_refuses_local_changes(monkeypatch, tmp_path: Path) -> None:
    status = updater.UpdateStatus(
        tmp_path,
        True,
        branch="main",
        commit="abc1234",
        upstream="origin/main",
        state="behind",
        behind=1,
        has_local_changes=True,
    )
    monkeypatch.setattr(updater, "_get_git_update_status", lambda *a, **k: status)

    result = pull_latest(tmp_path)

    assert result.success is False
    assert "Local changes" in result.message


def test_pull_latest_runs_ff_only_when_safe(monkeypatch, tmp_path: Path) -> None:
    status = updater.UpdateStatus(
        tmp_path,
        True,
        branch="main",
        commit="abc1234",
        upstream="origin/main",
        state="behind",
        behind=1,
    )
    calls: list[list[str]] = []

    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            sha = "oldsha1234567890" if calls.count(args) == 1 else "newsha1234567890"
            return GitCommandResult(["git", *args], 0, f"{sha}\n", "")
        if args == ["pull", "--ff-only"]:
            return GitCommandResult(["git", *args], 0, "Fast-forward\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "_get_git_update_status", lambda *a, **k: status)
    monkeypatch.setattr(updater, "run_git_command", fake_git)

    result = pull_latest(tmp_path)

    assert result.success is True
    assert result.old_commit == "oldsha1234567890"
    assert result.new_commit == "newsha1234567890"
    assert ["pull", "--ff-only"] in calls
