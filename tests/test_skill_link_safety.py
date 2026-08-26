"""Link containment for the skill boundary, on every supported Python.

Two things are proven here. First, that the containment decision is made by
one canonical helper (:func:`aura.paths.is_link_like`) written against APIs
that exist on Python 3.10 — Aura's declared minimum — rather than the 3.12+
``Path.exists(follow_symlinks=...)`` / ``Path.is_junction`` family. Second,
that the decision is actually applied everywhere a link could smuggle a path
out of a skill's own directory: the folder handed to an import, the tree it
stages, discovery of installed skills, the root and every component of a
resource read, and any target about to be deleted or replaced.

Windows junctions are covered twice: once through a mocked ``os.lstat`` so
the policy is verified on any platform, and once for real when the OS can
create one.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aura.paths import first_link_like_component, is_link_like
from aura.skills.identity import InstallScope
from aura.skills.importer import SkillImporter, SkillImportError
from aura.skills.library import SkillLibrary
from aura.skills.resources import SkillResourceError, resolve_skill_resource

#: Python 3.12+ (or later) spellings that must not creep back into the skill
#: boundary while Aura declares ``requires-python = ">=3.10"``.
_TOO_NEW_APIS = (
    "follow_symlinks=",
    "is_junction",
    "isjunction",
    "walk_up=",
    "case_sensitive=",
    "recurse_symlinks=",
    "itertools.batched",
    "StrEnum",
    "datetime.UTC",
    "hashlib.file_digest",
    "contextlib.chdir",
    "onexc=",
)

_GUARDED_SOURCES = (
    Path("aura/paths.py"),
    *sorted(Path("aura/skills").glob("*.py")),
)


def _write_skill(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a linkable skill\n---\n# {name}\n\nBody text.\n", encoding="utf-8"
    )
    (directory / "references").mkdir(exist_ok=True)
    (directory / "references" / "api.md").write_text("# API reference\n", encoding="utf-8")
    return directory


def _library(tmp_path: Path, **dirs: Path) -> SkillLibrary:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SkillLibrary(
        workspace,
        project_dir=dirs.get("project_dir", tmp_path / "project_authored"),
        personal_dir=dirs.get("personal_dir", tmp_path / "personal_authored"),
        bundled_dir=dirs.get("bundled_dir", tmp_path / "bundled"),
    )


def _try_symlink(link: Path, target: Path, *, directory: bool) -> None:
    """Create a symlink or skip the test when the OS will not allow it."""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlink creation is unavailable here: {exc}")


def _try_junction(link: Path, target: Path) -> None:
    """Create a real Windows junction, or skip when that is not possible."""
    if os.name != "nt":  # pragma: no cover - platform dependent
        pytest.skip("junctions only exist on Windows")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not link.exists():  # pragma: no cover - platform dependent
        pytest.skip(f"could not create a junction here: {result.stderr.strip() or result.stdout.strip()}")


class _FakeLinkStat:
    """Minimal ``os.lstat`` result shaped like a Windows reparse point."""

    def __init__(self, *, attributes: int, tag: int | None) -> None:
        self.st_mode = stat.S_IFDIR | 0o755
        self.st_file_attributes = attributes
        if tag is not None:
            self.st_reparse_tag = tag


def _patch_lstat_as_junction(
    monkeypatch: pytest.MonkeyPatch, junction: Path, *, tag: int | None = 0xA0000003
) -> None:
    """Make exactly *junction* look like a junction to ``os.lstat``.

    Everything else keeps its real stat result, so the code under test walks
    a genuine directory tree and only the one path is a link.
    """
    real_lstat = os.lstat
    wanted = os.path.normcase(os.path.abspath(str(junction)))

    def fake_lstat(path, *args, **kwargs):
        try:
            candidate = os.path.normcase(os.path.abspath(os.fspath(path)))
        except TypeError:  # pragma: no cover - file descriptors are never used here
            return real_lstat(path, *args, **kwargs)
        if candidate == wanted:
            return _FakeLinkStat(attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=tag)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake_lstat)


# ── Python 3.10 compatibility ───────────────────────────────────────────────


def _executable_source(path: Path) -> str:
    """The file's code with comments and string literals removed.

    Docstrings name the newer APIs on purpose — to say why they are not used
    — so the check has to look at what actually runs, not at the prose.
    """
    import io
    import tokenize

    pieces: list[str] = []
    with io.StringIO(path.read_text(encoding="utf-8")) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
    return "".join(pieces)


def test_skill_boundary_uses_no_python_312_only_apis() -> None:
    """Aura declares >=3.10; the skill boundary must not need a newer runtime."""
    offenders: list[str] = []
    for source in _GUARDED_SOURCES:
        code = _executable_source(source)
        offenders.extend(f"{source}: {api}" for api in _TOO_NEW_APIS if api in code)
    assert offenders == [], f"Python 3.12+ APIs found in the skill boundary: {offenders}"


def test_resource_resolution_never_calls_exists_with_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce Python 3.10's signature: ``Path.exists()`` takes no keywords."""
    directory = _write_skill(tmp_path / "project_authored", "py310-skill")
    real_exists = Path.exists

    def exists_310(self: Path, **kwargs: object) -> bool:
        if kwargs:
            raise TypeError("Path.exists() takes no keyword arguments on Python 3.10")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", exists_310)

    resolved = resolve_skill_resource(directory, "references/api.md")
    assert resolved.is_file()


def test_link_inspection_is_lstat_based_and_never_raises(tmp_path: Path) -> None:
    """The canonical helper answers for real files, missing paths, and junk."""
    real_file = tmp_path / "plain.txt"
    real_file.write_text("content", encoding="utf-8")

    assert is_link_like(real_file) is False
    assert is_link_like(tmp_path) is False
    assert is_link_like(tmp_path / "does-not-exist") is False
    assert is_link_like(tmp_path / "no-such-dir" / "deeper" / "file.txt") is False


def test_symlink_is_link_like(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    _try_symlink(link, target, directory=False)

    assert is_link_like(link) is True
    assert is_link_like(target) is False


def test_mocked_windows_junction_is_link_like(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform-independent: a mount-point reparse tag is a link anywhere."""
    junction = tmp_path / "junction"
    junction.mkdir()
    _patch_lstat_as_junction(monkeypatch, junction)

    assert is_link_like(junction) is True
    assert is_link_like(tmp_path) is False


def test_mocked_reparse_point_without_a_tag_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "unknown-reparse"
    directory.mkdir()
    _patch_lstat_as_junction(monkeypatch, directory, tag=None)

    assert is_link_like(directory) is True


def test_non_redirecting_reparse_point_is_not_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A OneDrive-style placeholder is a normal file, not an escape hatch."""
    placeholder = tmp_path / "cloud-file"
    placeholder.mkdir()
    _patch_lstat_as_junction(monkeypatch, placeholder, tag=0x9000001A)  # IO_REPARSE_TAG_CLOUD

    assert is_link_like(placeholder) is False


def test_first_link_like_component_checks_the_root_before_the_parts(tmp_path: Path) -> None:
    real = _write_skill(tmp_path / "project_authored", "walked")
    link_root = tmp_path / "linked-root"
    _try_symlink(link_root, real, directory=True)

    assert first_link_like_component(real, ("references", "api.md")) is None
    assert first_link_like_component(link_root, ("references", "api.md")) == link_root


@pytest.mark.skipif(os.name != "nt", reason="junctions only exist on Windows")
def test_real_windows_junction_is_link_like(tmp_path: Path) -> None:
    target = tmp_path / "real-target"
    target.mkdir()
    junction = tmp_path / "real-junction"
    _try_junction(junction, target)

    assert is_link_like(junction) is True
    assert is_link_like(target) is False


# ── resource resolution ─────────────────────────────────────────────────────


def test_resource_root_that_is_a_symlink_is_rejected(tmp_path: Path) -> None:
    """A linked skill root never lends its target out as a trusted root."""
    real = _write_skill(tmp_path / "elsewhere", "sneaky")
    link_root = tmp_path / "project_authored" / "sneaky"
    link_root.parent.mkdir(parents=True)
    _try_symlink(link_root, real, directory=True)

    with pytest.raises(SkillResourceError, match="symlink or junction"):
        resolve_skill_resource(link_root, "references/api.md")


def test_resource_root_that_is_a_mocked_junction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_skill(tmp_path / "project_authored", "junction-root")
    _patch_lstat_as_junction(monkeypatch, directory)

    with pytest.raises(SkillResourceError, match="symlink or junction"):
        resolve_skill_resource(directory, "references/api.md")


def test_nested_symlinked_directory_component_is_rejected(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path / "project_authored", "nested")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.md").write_text("top secret", encoding="utf-8")
    _try_symlink(directory / "linked-refs", outside, directory=True)

    with pytest.raises(SkillResourceError, match="symlink or junction"):
        resolve_skill_resource(directory, "linked-refs/secrets.md")


def test_nested_mocked_junction_component_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write_skill(tmp_path / "project_authored", "nested-junction")
    hop = directory / "references"
    _patch_lstat_as_junction(monkeypatch, hop)

    with pytest.raises(SkillResourceError, match="symlink or junction"):
        resolve_skill_resource(directory, "references/api.md")


@pytest.mark.skipif(os.name != "nt", reason="junctions only exist on Windows")
def test_real_windows_junction_resource_component_is_rejected(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path / "project_authored", "real-junction-skill")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.md").write_text("top secret", encoding="utf-8")
    _try_junction(directory / "escape", outside)

    with pytest.raises(SkillResourceError, match="symlink or junction"):
        resolve_skill_resource(directory, "escape/secrets.md")


# ── installed-skill discovery ───────────────────────────────────────────────


def test_linked_skill_directory_is_diagnosed_and_excluded(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    real = _write_skill(tmp_path / "elsewhere", "planted")
    _write_skill(project_dir, "legit")
    _try_symlink(project_dir / "planted", real, directory=True)

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert [s.install_id for s in skills] == ["project:legit"]
    assert any(d.code == "linked_skill_directory" and d.is_error for d in diagnostics)


def test_mocked_junction_skill_directory_is_diagnosed_and_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(project_dir, "legit")
    planted = _write_skill(project_dir, "planted")
    _patch_lstat_as_junction(monkeypatch, planted)

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert [s.install_id for s in skills] == ["project:legit"]
    assert any(d.code == "linked_skill_directory" for d in diagnostics)


def test_linked_skill_directory_stays_visible_to_management(tmp_path: Path) -> None:
    """Excluded from runtime, still listable so a GUI can clean it up."""
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    real = _write_skill(tmp_path / "elsewhere", "planted")
    _try_symlink(project_dir / "planted", real, directory=True)

    lib = _library(tmp_path, project_dir=project_dir)
    rows = lib.list_installed(InstallScope.PROJECT)

    assert [(row.installed_id, row.valid) for row in rows] == [("project:planted", False)]
    assert any(d.code == "linked_skill_directory" for d in rows[0].diagnostics)
    inspected = lib.inspect("project:planted")
    assert inspected is not None and inspected.valid is False


def test_linked_scope_directory_yields_no_skills(tmp_path: Path) -> None:
    real_dir = tmp_path / "real_project_authored"
    _write_skill(real_dir, "hidden")
    linked_scope = tmp_path / "linked_project_authored"
    _try_symlink(linked_scope, real_dir, directory=True)

    lib = _library(tmp_path, project_dir=linked_scope)
    skills, diagnostics = lib.discover_effective_skills()

    assert skills == []
    assert any(d.code == "linked_skill_root" for d in diagnostics)


def test_linked_skill_manifest_is_excluded(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = project_dir / "borrowed"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside-SKILL.md"
    outside.write_text("---\nname: borrowed\ndescription: x\n---\nBody.\n", encoding="utf-8")
    _try_symlink(directory / "SKILL.md", outside, directory=False)

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert skills == []
    assert any(d.code == "linked_skill_manifest" for d in diagnostics)


# ── destructive targets ─────────────────────────────────────────────────────


def test_uninstalling_a_linked_skill_removes_the_link_not_its_target(tmp_path: Path) -> None:
    """rmtree through a junction deletes the target's files — never recurse."""
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    real = _write_skill(tmp_path / "elsewhere", "planted")
    link = project_dir / "planted"
    _try_symlink(link, real, directory=True)

    lib = _library(tmp_path, project_dir=project_dir)
    lib.uninstall("project:planted")

    assert not os.path.lexists(link)
    assert (real / "SKILL.md").is_file()
    assert (real / "references" / "api.md").is_file()


@pytest.mark.skipif(os.name != "nt", reason="junctions only exist on Windows")
def test_uninstalling_a_real_junction_skill_preserves_the_target(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    real = _write_skill(tmp_path / "elsewhere", "planted")
    link = project_dir / "planted"
    _try_junction(link, real)

    lib = _library(tmp_path, project_dir=project_dir)
    lib.uninstall("project:planted")

    assert not os.path.lexists(link)
    assert (real / "SKILL.md").is_file()


def test_install_refuses_to_replace_through_a_linked_destination(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    real = _write_skill(tmp_path / "elsewhere", "occupied")
    link = project_dir / "occupied"
    _try_symlink(link, real, directory=True)

    source = _write_skill(tmp_path / "sources", "occupied")
    importer = SkillImporter(_library(tmp_path, project_dir=project_dir))
    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    try:
        with pytest.raises(SkillImportError, match="symlink or junction"):
            importer.install(preview, replace=True)
    finally:
        importer.cleanup(preview)

    assert os.path.lexists(link)
    assert (real / "SKILL.md").read_text(encoding="utf-8") == (
        (tmp_path / "elsewhere" / "occupied" / "SKILL.md").read_text(encoding="utf-8")
    )


# ── import staging ──────────────────────────────────────────────────────────


def test_preview_from_folder_rejects_a_linked_source_root(tmp_path: Path) -> None:
    """Checked before ``resolve()``, which would erase the evidence."""
    real = _write_skill(tmp_path / "sources", "linked-source")
    link = tmp_path / "link-to-source"
    _try_symlink(link, real, directory=True)

    importer = SkillImporter(_library(tmp_path))
    with pytest.raises(SkillImportError, match="symlink or junction"):
        importer.preview_from_folder(link, destination_scope=InstallScope.PROJECT)


def test_preview_from_folder_rejects_a_mocked_junction_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_skill(tmp_path / "sources", "junction-source")
    _patch_lstat_as_junction(monkeypatch, source)

    importer = SkillImporter(_library(tmp_path))
    with pytest.raises(SkillImportError, match="symlink or junction"):
        importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)


def test_staging_rejects_a_nested_linked_directory(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "sources", "nested-link")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.md").write_text("top secret", encoding="utf-8")
    _try_symlink(source / "scripts", outside, directory=True)

    importer = SkillImporter(_library(tmp_path))
    with pytest.raises(SkillImportError, match="symlink or junction"):
        importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)


def test_no_staging_directory_survives_a_rejected_import(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "sources", "doomed")
    _try_symlink(source / "linked.md", tmp_path / "outside.md", directory=False)
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")

    before = _temp_staging_dirs()
    importer = SkillImporter(_library(tmp_path))
    with pytest.raises(SkillImportError):
        importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)

    assert _temp_staging_dirs() == before


def _temp_staging_dirs() -> set[str]:
    import tempfile

    root = Path(tempfile.gettempdir())
    try:
        return {p.name for p in root.iterdir() if p.name.startswith("aura-skill-import-")}
    except OSError:  # pragma: no cover - platform dependent
        return set()


def test_python_version_under_test_is_supported() -> None:
    """Documents the interpreter this run actually exercised."""
    assert sys.version_info >= (3, 10)
