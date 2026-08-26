"""SkillImporter: acquire into staging, validate, preview, install explicitly.

Covers folder/ZIP/mocked-GitHub previews and installs, explicit replacement,
atomic install failure, archive/folder safety limits (traversal, absolute
paths, symlinks, ambiguous archives, size/count limits), and that nothing
staged or installed is ever executed.

Also covers the two windows a preview cannot speak for: the destination can
gain a skill between preview and install, and the staging directory can be
rewritten in the same gap. Both are re-derived at install time, so neither
the preview's conflict flag nor its approved content is authority over what
is actually on disk when the filesystem is finally touched.

GitHub downloads are exercised through an ``httpx`` mock transport, so the
real streaming, byte-counting, and cleanup code runs with no network.
"""
from __future__ import annotations

import dataclasses
import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from aura.skills import archive as archive_module
from aura.skills import github_source as github_module
from aura.skills.archive import MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_MEMBERS, ArchiveError, safe_extract_zip
from aura.skills.github_source import (
    GitHubImportError,
    GitHubSkillFetcher,
    GitHubTarget,
    parse_github_url,
)
from aura.skills.identity import InstallScope
from aura.skills.importer import SkillImporter, SkillImportError
from aura.skills.library import SkillLibrary


def _write_skill_folder(root: Path, name: str, *, frontmatter: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    front = frontmatter if frontmatter is not None else f"name: {name}\ndescription: a test skill\n"
    (directory / "SKILL.md").write_text(f"---\n{front}---\n# {name}\n\nBody text for the skill.\n", encoding="utf-8")
    return directory


def _library(tmp_path: Path) -> SkillLibrary:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SkillLibrary(
        workspace,
        project_dir=tmp_path / "project_authored",
        personal_dir=tmp_path / "personal_authored",
        bundled_dir=tmp_path / "bundled",
    )


def _zip_of(source: Path, dest: Path, *, root_name: str | None = None) -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        for path in source.rglob("*"):
            if path.is_file():
                rel = path.relative_to(source)
                arcname = f"{root_name}/{rel.as_posix()}" if root_name else rel.as_posix()
                zf.write(path, arcname)
    return dest


# ── folder import ────────────────────────────────────────────────────────────


def test_preview_from_folder_reports_name_and_description(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "cool-skill")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    try:
        assert preview.ok
        assert preview.name == "cool-skill"
        assert preview.description == "a test skill"
        assert preview.destination_scope == InstallScope.PROJECT
        assert preview.conflict is False
        assert preview.file_count == 1
    finally:
        importer.cleanup(preview)


def test_install_from_folder_preview_creates_the_skill(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "installable")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    summary = importer.install(preview)

    assert summary.installed_id == "project:installable"
    installed_skill_md = lib.dir_for_scope(InstallScope.PROJECT) / "installable" / "SKILL.md"
    assert installed_skill_md.is_file()
    # Staging is cleaned up after install.
    assert not preview.staging_root.exists()


def test_install_never_touches_the_original_source(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "readonly-source")
    original = (source / "SKILL.md").read_text(encoding="utf-8")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    importer.install(preview)

    assert (source / "SKILL.md").read_text(encoding="utf-8") == original


def test_conflict_requires_explicit_replacement(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "existing")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    first = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    importer.install(first)

    source2 = _write_skill_folder(tmp_path / "sources2", "existing", frontmatter="name: existing\ndescription: v2\n")
    second = importer.preview_from_folder(source2, destination_scope=InstallScope.PROJECT)
    assert second.conflict is True

    with pytest.raises(SkillImportError):
        importer.install(second)

    # Explicit replacement succeeds and actually replaces the content.
    third = importer.preview_from_folder(source2, destination_scope=InstallScope.PROJECT)
    importer.install(third, replace=True)
    installed = lib.dir_for_scope(InstallScope.PROJECT) / "existing" / "SKILL.md"
    assert "description: v2" in installed.read_text(encoding="utf-8")
    skills, _ = lib.discover_effective_skills()
    assert skills[0].description == "v2"


def test_missing_name_and_description_are_warnings_not_blockers(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "bare-skill", frontmatter="")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert preview.ok
    assert any(d.code == "missing_name" for d in preview.diagnostics)
    assert any(d.code == "missing_description" for d in preview.diagnostics)


def test_invalid_declared_name_blocks_install(tmp_path: Path) -> None:
    source = _write_skill_folder(
        tmp_path / "sources", "weird", frontmatter="name: Not A Valid Name!\ndescription: x\n"
    )
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert not preview.ok
    assert any(d.code == "invalid_name" and d.is_error for d in preview.diagnostics)
    with pytest.raises(SkillImportError):
        importer.install(preview)


def test_folder_import_rejects_symlinks(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation requires elevated privileges on Windows CI")
    source = _write_skill_folder(tmp_path / "sources", "with-link")
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (source / "link.txt").symlink_to(target)

    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    with pytest.raises(SkillImportError):
        importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)


def test_cannot_install_into_bundled_scope(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "cant-be-bundled")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    preview = importer.preview_from_folder(source, destination_scope=InstallScope.BUNDLED)
    with pytest.raises(SkillImportError):
        importer.install(preview)


def test_atomic_install_rolls_back_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_skill_folder(tmp_path / "sources", "atomic-test")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    first = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    importer.install(first)

    original_text = (lib.dir_for_scope(InstallScope.PROJECT) / "atomic-test" / "SKILL.md").read_text(encoding="utf-8")

    source2 = _write_skill_folder(tmp_path / "sources2", "atomic-test")
    second = importer.preview_from_folder(source2, destination_scope=InstallScope.PROJECT)

    import aura.skills.importer as importer_module

    def _boom(_src, _dst):
        raise OSError("simulated failure copying the new content into place")

    monkeypatch.setattr(importer_module.shutil, "copytree", _boom)
    with pytest.raises(OSError):
        importer.install(second, replace=True)

    # The original installation is intact — never left half-replaced.
    surviving = lib.dir_for_scope(InstallScope.PROJECT) / "atomic-test" / "SKILL.md"
    assert surviving.is_file()
    assert surviving.read_text(encoding="utf-8") == original_text


# ── zip import ───────────────────────────────────────────────────────────────


def test_preview_from_zip_finds_the_unambiguous_skill_root(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "zipped-skill")
    archive = _zip_of(source, tmp_path / "zipped-skill.zip", root_name="zipped-skill")

    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    preview = importer.preview_from_zip(archive, destination_scope=InstallScope.PERSONAL)
    try:
        assert preview.ok
        assert preview.name == "zipped-skill"
    finally:
        importer.cleanup(preview)


def test_ambiguous_zip_with_two_skill_dirs_is_rejected(tmp_path: Path) -> None:
    staging = tmp_path / "multi"
    _write_skill_folder(staging, "one")
    _write_skill_folder(staging, "two")
    archive = _zip_of(staging, tmp_path / "multi.zip")

    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    with pytest.raises(SkillImportError, match="ambiguous"):
        importer.preview_from_zip(archive, destination_scope=InstallScope.PERSONAL)


def test_zip_with_no_skill_md_is_rejected(tmp_path: Path) -> None:
    staging = tmp_path / "empty_zip_src"
    staging.mkdir()
    (staging / "readme.txt").write_text("nothing here", encoding="utf-8")
    archive = _zip_of(staging, tmp_path / "empty.zip")

    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    with pytest.raises(SkillImportError):
        importer.preview_from_zip(archive, destination_scope=InstallScope.PERSONAL)


def test_zip_traversal_entry_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
    with pytest.raises(ArchiveError):
        safe_extract_zip(archive_path, tmp_path / "extract_traversal")


def test_zip_absolute_path_entry_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "absolute.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    with pytest.raises(ArchiveError):
        safe_extract_zip(archive_path, tmp_path / "extract_absolute")


def test_zip_symlink_member_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        zf.writestr(info, "target.txt")
    with pytest.raises(ArchiveError):
        safe_extract_zip(archive_path, tmp_path / "extract_symlink")


def test_zip_member_count_limit_is_enforced(tmp_path: Path) -> None:
    archive_path = tmp_path / "toomany.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        for i in range(MAX_ARCHIVE_MEMBERS + 1):
            zf.writestr(f"file_{i}.txt", "x")
    with pytest.raises(ArchiveError, match="too many entries"):
        safe_extract_zip(archive_path, tmp_path / "extract_toomany")


def test_zip_member_size_limit_is_enforced(tmp_path: Path) -> None:
    archive_path = tmp_path / "toobig.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("huge.bin", b"0" * (MAX_ARCHIVE_MEMBER_BYTES + 1))
    with pytest.raises(ArchiveError, match="too large"):
        safe_extract_zip(archive_path, tmp_path / "extract_toobig")


def test_malformed_zip_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "not_a_zip.zip"
    bad.write_bytes(b"this is not a zip file at all")
    with pytest.raises(ArchiveError):
        safe_extract_zip(bad, tmp_path / "extract_bad")


def test_rejected_archive_leaves_destination_untouched(tmp_path: Path) -> None:
    archive_path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("good.txt", "fine")
        zf.writestr("../escape.txt", "bad")
    destination = tmp_path / "extract_mixed"
    with pytest.raises(ArchiveError):
        safe_extract_zip(archive_path, destination)
    assert not (destination / "good.txt").exists()


# ── github import (mocked — no network) ─────────────────────────────────────


def test_parse_github_url_root_form() -> None:
    target = parse_github_url("https://github.com/acme/widgets")
    assert target == GitHubTarget(owner="acme", repo="widgets", ref="HEAD", subpath="")


def test_parse_github_url_tree_form() -> None:
    target = parse_github_url("https://github.com/acme/widgets/tree/main/skills/my-skill")
    assert target == GitHubTarget(owner="acme", repo="widgets", ref="main", subpath="skills/my-skill")


def test_parse_github_url_rejects_unsupported_urls() -> None:
    with pytest.raises(GitHubImportError):
        parse_github_url("https://gitlab.com/acme/widgets")
    with pytest.raises(GitHubImportError):
        parse_github_url("https://github.com/acme/widgets/blob/main/README.md")


class _FakeGitHubFetcher:
    """Test double standing in for GitHubSkillFetcher — no network access."""

    def __init__(self, skill_dir: Path) -> None:
        self._skill_dir = skill_dir

    def fetch(self, target: GitHubTarget, staging_root: Path) -> Path:
        return self._skill_dir


def test_preview_from_github_uses_the_injected_fetcher_never_the_network(tmp_path: Path) -> None:
    fake_repo_skill = _write_skill_folder(tmp_path / "fake_repo", "remote-skill")
    lib = _library(tmp_path)
    importer = SkillImporter(lib, github_fetcher=_FakeGitHubFetcher(fake_repo_skill))

    preview = importer.preview_from_github(
        "https://github.com/acme/widgets/tree/main/remote-skill", destination_scope=InstallScope.PERSONAL
    )
    try:
        assert preview.ok
        assert preview.name == "remote-skill"
    finally:
        importer.cleanup(preview)


def test_github_fetch_failure_is_a_skill_import_error(tmp_path: Path) -> None:
    class _FailingFetcher:
        def fetch(self, target, staging_root):
            raise GitHubImportError("network error fetching acme/widgets: boom")

    lib = _library(tmp_path)
    importer = SkillImporter(lib, github_fetcher=_FailingFetcher())
    with pytest.raises(SkillImportError):
        importer.preview_from_github("https://github.com/acme/widgets", destination_scope=InstallScope.PERSONAL)


# ── the destination is re-resolved at install time ──────────────────────────


def test_destination_appearing_after_preview_is_rejected_without_replace(tmp_path: Path) -> None:
    """The preview said "no conflict"; by install time that is no longer true."""
    source = _write_skill_folder(tmp_path / "sources", "racy")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert preview.conflict is False

    interloper = _write_skill_folder(
        lib.dir_for_scope(InstallScope.PROJECT), "racy", frontmatter="name: racy\ndescription: theirs\n"
    )

    try:
        with pytest.raises(SkillImportError, match="already exists"):
            importer.install(preview)
    finally:
        importer.cleanup(preview)

    assert "description: theirs" in (interloper / "SKILL.md").read_text(encoding="utf-8")


def test_the_same_race_installs_only_with_explicit_replacement(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "racy", frontmatter="name: racy\ndescription: mine\n")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert preview.conflict is False
    interloper = _write_skill_folder(
        lib.dir_for_scope(InstallScope.PROJECT), "racy", frontmatter="name: racy\ndescription: theirs\n"
    )

    summary = importer.install(preview, replace=True)

    assert summary.installed_id == "project:racy"
    assert "description: mine" in (interloper / "SKILL.md").read_text(encoding="utf-8")


def test_a_stale_conflict_flag_is_not_authority_in_either_direction(tmp_path: Path) -> None:
    """A preview that recorded a conflict since resolved still installs cleanly."""
    source = _write_skill_folder(tmp_path / "sources", "was-conflicting")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    stale = dataclasses.replace(preview, conflict=True)

    summary = importer.install(stale)
    assert summary.installed_id == "project:was-conflicting"


def test_legacy_json_name_taken_after_preview_still_conflicts(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "shared-name")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    project_dir = lib.dir_for_scope(InstallScope.PROJECT)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "shared-name.json").write_text('{"text": "legacy guard"}', encoding="utf-8")

    try:
        with pytest.raises(SkillImportError, match="already exists"):
            importer.install(preview)
    finally:
        importer.cleanup(preview)


# ── the staged content is re-derived at install time ────────────────────────


def test_staged_skill_rewritten_after_preview_is_rejected(tmp_path: Path) -> None:
    source = _write_skill_folder(tmp_path / "sources", "honest")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    (preview.staging_dir / "SKILL.md").write_text(
        "---\nname: impostor\ndescription: swapped after approval\n---\nDifferent body.\n", encoding="utf-8"
    )

    try:
        with pytest.raises(SkillImportError, match="no longer matches"):
            importer.install(preview)
    finally:
        importer.cleanup(preview)

    project_dir = lib.dir_for_scope(InstallScope.PROJECT)
    assert not (project_dir / "honest").exists()
    assert not (project_dir / "impostor").exists()


def test_a_script_added_to_staging_after_preview_is_rejected(tmp_path: Path) -> None:
    """The safety finding the user approved must still describe the content."""
    source = _write_skill_folder(tmp_path / "sources", "clean")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert preview.has_scripts_or_executables is False
    (preview.staging_dir / "scripts").mkdir()
    (preview.staging_dir / "scripts" / "payload.sh").write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")

    try:
        with pytest.raises(SkillImportError, match="no longer matches"):
            importer.install(preview)
    finally:
        importer.cleanup(preview)

    assert not (lib.dir_for_scope(InstallScope.PROJECT) / "clean").exists()


def test_untouched_staging_installs_normally(tmp_path: Path) -> None:
    """Revalidation must not reject an ordinary, unmodified import."""
    source = _write_skill_folder(tmp_path / "sources", "unmodified")
    (source / "references").mkdir()
    (source / "references" / "api.md").write_text("# API\n", encoding="utf-8")
    lib = _library(tmp_path)
    importer = SkillImporter(lib)

    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    assert preview.fingerprint
    summary = importer.install(preview)

    assert summary.installed_id == "project:unmodified"
    assert summary.has_resources is True


# ── bounded github download (mock transport — no network) ───────────────────


def _zipball_bytes(skill_name: str, *, repo_root: str = "widgets-main") -> bytes:
    """A GitHub-shaped zipball: one wrapper directory holding one skill."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            f"{repo_root}/{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: a remote skill\n---\n# {skill_name}\n\nBody.\n",
        )
    return buffer.getvalue()


def _fetcher_for(handler) -> GitHubSkillFetcher:
    return GitHubSkillFetcher(transport=httpx.MockTransport(handler))


def _staging_files(staging_root: Path) -> list[str]:
    return sorted(p.relative_to(staging_root).as_posix() for p in staging_root.rglob("*"))


def test_compressed_download_limit_is_centralized() -> None:
    """One constant, defined beside the other archive limits."""
    assert github_module.MAX_COMPRESSED_DOWNLOAD_BYTES is archive_module.MAX_COMPRESSED_DOWNLOAD_BYTES
    assert archive_module.MAX_COMPRESSED_DOWNLOAD_BYTES > 0


def test_oversized_content_length_is_refused_before_the_body_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_module, "MAX_COMPRESSED_DOWNLOAD_BYTES", 4096)
    body_started = False

    def body():
        nonlocal body_started
        body_started = True
        yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "999999999"}, content=body())

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="too large"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "widgets", "HEAD", ""), staging_root)

    assert body_started is False
    assert _staging_files(staging_root) == []


def test_stream_exceeding_the_limit_is_stopped_without_a_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No header at all: the count of real bytes is what stops it."""
    monkeypatch.setattr(github_module, "MAX_COMPRESSED_DOWNLOAD_BYTES", 4096)
    sent = 0

    def body():
        nonlocal sent
        for _ in range(512):
            sent += 1024
            yield b"y" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="exceeded"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "widgets", "HEAD", ""), staging_root)

    # Stopped within a transport buffer of the limit — nowhere near the 512 KiB
    # the server was willing to send — and nothing partial was left behind.
    assert sent < 512 * 1024
    assert sent <= 4096 + 128 * 1024
    assert _staging_files(staging_root) == []


def test_a_lying_content_length_does_not_get_a_free_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Header claims 10 bytes and then sends megabytes; the counter wins."""
    monkeypatch.setattr(github_module, "MAX_COMPRESSED_DOWNLOAD_BYTES", 4096)

    def body():
        for _ in range(50):
            yield b"z" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "10"}, content=body())

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="exceeded"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "widgets", "HEAD", ""), staging_root)

    assert _staging_files(staging_root) == []


def test_a_bounded_download_extracts_the_requested_skill_directory(tmp_path: Path) -> None:
    payload = _zipball_bytes("remote-skill")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "codeload.github.com"
        return httpx.Response(200, content=payload)

    staging_root = tmp_path / "staging"
    resolved = _fetcher_for(handler).fetch(
        GitHubTarget("acme", "widgets", "main", "remote-skill"), staging_root
    )

    assert (resolved / "SKILL.md").is_file()
    # The compressed download itself is not left lying around.
    assert not (staging_root / "github-download.zip").exists()


def test_a_failed_status_leaves_no_partial_staging(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="HTTP 404"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "private", "HEAD", ""), staging_root)

    assert _staging_files(staging_root) == []


def test_a_corrupt_download_leaves_no_partial_staging(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not a zip file at all")

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="invalid"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "widgets", "HEAD", ""), staging_root)

    assert _staging_files(staging_root) == []


def test_a_network_error_becomes_a_github_import_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    staging_root = tmp_path / "staging"
    with pytest.raises(GitHubImportError, match="network error"):
        _fetcher_for(handler).fetch(GitHubTarget("acme", "widgets", "HEAD", ""), staging_root)

    assert _staging_files(staging_root) == []


def test_importer_installs_a_mock_transport_github_skill_end_to_end(tmp_path: Path) -> None:
    payload = _zipball_bytes("remote-skill")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    lib = _library(tmp_path)
    importer = SkillImporter(lib, github_fetcher=_fetcher_for(handler))
    preview = importer.preview_from_github(
        "https://github.com/acme/widgets/tree/main/remote-skill", destination_scope=InstallScope.PERSONAL
    )
    summary = importer.install(preview)

    assert summary.installed_id == "personal:remote-skill"


# ── never executes imported content ─────────────────────────────────────────


def test_import_never_executes_a_staged_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malicious script in scripts/ must never run during preview or install."""
    marker = tmp_path / "executed.txt"
    source = _write_skill_folder(tmp_path / "sources", "with-script")
    (source / "scripts").mkdir()
    script = source / "scripts" / "setup.py"
    script.write_text(
        f"import pathlib; pathlib.Path(r'{marker}').write_text('EXECUTED')\n",
        encoding="utf-8",
    )

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess/exec must never be invoked during import")

    monkeypatch.setattr("subprocess.run", _fail_if_called, raising=False)
    monkeypatch.setattr("os.system", _fail_if_called, raising=False)

    lib = _library(tmp_path)
    importer = SkillImporter(lib)
    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    importer.install(preview)

    assert not marker.exists()
    assert preview.has_scripts_or_executables is True
