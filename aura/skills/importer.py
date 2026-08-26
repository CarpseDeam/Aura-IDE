"""Import staging, preview, and installation for one skill at a time.

Every import follows the same four steps regardless of source: acquire into
temporary staging, validate without touching installed state, return a
preview, install only through an explicit call. Nothing here executes
anything staged — scripts are inert content, read only through the
production ``read_skill_resource`` tool once a skill is installed and
activated.

A preview describes the world as it was when the preview was taken, and is
never treated as authority over the world at install time. Staging lives in
a temp directory and the destination lives in the user's skill folder; both
can change in between. :meth:`SkillImporter.install` therefore re-derives
the staged content and re-resolves the destination immediately before it
mutates anything, and refuses if either no longer matches what was approved.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from aura.paths import is_link_like
from aura.skills.archive import (
    MAX_ARCHIVE_MEMBER_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    ArchiveError,
    find_skill_root,
    safe_extract_zip,
)
from aura.skills.diagnostics import SkillDiagnostic, error, warning
from aura.skills.frontmatter import parse_skill_markdown
from aura.skills.github_source import GitHubImportError, GitHubSkillFetcher, parse_github_url
from aura.skills.identity import InstallScope, is_valid_skill_name, normalize_skill_name
from aura.skills.library import InstalledSkillSummary, SkillLibrary

logger = logging.getLogger(__name__)

_EXECUTABLE_SUFFIXES = {".sh", ".bat", ".cmd", ".ps1", ".exe", ".py", ".rb", ".pl", ".js", ".vbs"}
_RESOURCE_DIR_NAMES = ("scripts", "references", "assets")
_FINGERPRINT_CHUNK = 1024 * 1024


class SkillImportError(Exception):
    """An import could not be staged, validated, or installed."""


@dataclass(frozen=True)
class StagedScan:
    """What one pass over a staged skill directory found."""

    file_count: int
    resource_dirs: tuple[str, ...]
    has_scripts_or_executables: bool
    #: Content hash of the whole staged tree — every relative path and every
    #: byte. Recomputed at install time and compared against the previewed
    #: value, so staged content cannot change after it was approved.
    fingerprint: str


@dataclass(frozen=True)
class ImportPreview:
    """Result of validating a staged skill before it is installed."""

    staging_root: Path
    staging_dir: Path
    name: str
    description: str
    destination_scope: InstallScope
    conflict: bool
    file_count: int
    resource_dirs: tuple[str, ...]
    has_scripts_or_executables: bool
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    #: Content hash of the staged tree at preview time. Advisory to display,
    #: authoritative at install time (see :meth:`SkillImporter.install`).
    fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.name) and not any(d.is_error for d in self.diagnostics)


def _scan_staged_dir(staged_dir: Path) -> StagedScan:
    """Describe and fingerprint a staged skill tree, refusing any link inside it.

    Staging is produced by copying regular files or by extracting an archive
    that already rejected symlink members, so a link found here was planted
    afterwards — it is never followed, counted, or installed.
    """
    file_count = 0
    has_scripts = False
    resource_dirs = tuple(name for name in _RESOURCE_DIR_NAMES if (staged_dir / name).is_dir())
    digest = hashlib.sha256()
    for path in sorted(staged_dir.rglob("*"), key=lambda p: p.as_posix()):
        if is_link_like(path):
            raise SkillImportError(f"staged content contains a symlink or junction ({path}); refusing to install it")
        if not path.is_file():
            continue
        file_count += 1
        rel = path.relative_to(staged_dir)
        digest.update(rel.as_posix().encode("utf-8", "replace"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_FINGERPRINT_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise SkillImportError(f"could not read staged file '{path}': {exc}") from exc
        digest.update(b"\0")
        if rel.parts and rel.parts[0] == "scripts":
            has_scripts = True
        elif path.suffix.lower() in _EXECUTABLE_SUFFIXES:
            has_scripts = True
        elif os.name != "nt":
            try:
                has_scripts = has_scripts or os.access(path, os.X_OK)
            except OSError:
                pass
    return StagedScan(
        file_count=file_count,
        resource_dirs=resource_dirs,
        has_scripts_or_executables=has_scripts,
        fingerprint=digest.hexdigest(),
    )


def _validate_staged_skill(staged_dir: Path) -> tuple[str, str, list[SkillDiagnostic]]:
    """Validate a staged ``<dir>/SKILL.md`` for import. Returns (name, description, diagnostics)."""
    skill_md = staged_dir / "SKILL.md"
    if is_link_like(skill_md):
        return "", "", [error("linked_skill_manifest", "SKILL.md is a symlink or junction", str(skill_md))]
    if not skill_md.is_file():
        return "", "", [error("missing_skill_md", "SKILL.md not found", str(staged_dir))]
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return "", "", [error("unreadable", str(exc), str(skill_md))]

    parsed = parse_skill_markdown(raw, source=str(skill_md))
    diagnostics = list(parsed.diagnostics)
    if not parsed.ok:
        return "", "", diagnostics

    declared_name = parsed.metadata.get("name")
    if declared_name:
        name = declared_name
        if not is_valid_skill_name(name):
            diagnostics.append(
                error(
                    "invalid_name",
                    f"name '{declared_name}' must contain only lowercase letters, digits, '-' or '_'",
                    str(skill_md),
                )
            )
    else:
        name = normalize_skill_name(staged_dir.name)
        diagnostics.append(warning("missing_name", "SKILL.md has no 'name' field; using the folder name", str(skill_md)))
        if not is_valid_skill_name(name):
            diagnostics.append(error("invalid_name", f"folder name '{staged_dir.name}' is not a valid skill name", str(skill_md)))

    explicit_description = parsed.metadata.get("description")
    if not explicit_description:
        diagnostics.append(warning("missing_description", "SKILL.md has no 'description' field", str(skill_md)))
    from aura.skills.description import derive_skill_description

    description = derive_skill_description(parsed.body, explicit=explicit_description)
    if not description:
        diagnostics.append(error("missing_description", "no description available (declared or derivable)", str(skill_md)))

    return name, description, diagnostics


def _stage_folder(source: Path, staging_dir: Path) -> None:
    """Copy *source* into *staging_dir*, rejecting links and oversized trees.

    *source* itself is checked before anything walks it, so a junctioned or
    symlinked import folder is refused rather than silently importing
    whatever it points at.
    """
    if is_link_like(source):
        raise SkillImportError(f"'{source}' is a symlink or junction; refusing to import it")
    if not source.is_dir():
        raise SkillImportError(f"'{source}' is not a directory")
    staging_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    total_files = 0
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(source)
        dirnames.sort()
        for dirname in list(dirnames):
            if is_link_like(root_path / dirname):
                raise SkillImportError(
                    f"source contains a symlink or junction directory ({root_path / dirname}); refusing to import it"
                )
        dest_dir = staging_dir / rel_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        for filename in sorted(filenames):
            src_file = root_path / filename
            if is_link_like(src_file):
                raise SkillImportError(
                    f"source contains a symlink or junction file ({src_file}); refusing to import it"
                )
            total_files += 1
            if total_files > MAX_ARCHIVE_MEMBERS:
                raise SkillImportError(f"source has too many files (> {MAX_ARCHIVE_MEMBERS})")
            size = src_file.stat().st_size
            if size > MAX_ARCHIVE_MEMBER_BYTES:
                raise SkillImportError(f"source file '{src_file}' is too large (> {MAX_ARCHIVE_MEMBER_BYTES} bytes)")
            total_bytes += size
            if total_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise SkillImportError(f"source is too large in total (> {MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes)")
            shutil.copy2(src_file, dest_dir / filename)


class SkillImporter:
    """Stages, validates, previews, and installs one skill import at a time."""

    def __init__(self, library: SkillLibrary, *, github_fetcher: GitHubSkillFetcher | None = None) -> None:
        self._library = library
        self._github_fetcher = github_fetcher or GitHubSkillFetcher()

    def _new_staging_root(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="aura-skill-import-"))

    def _finish_preview(self, staging_root: Path, staged_dir: Path, destination_scope: InstallScope) -> ImportPreview:
        name, description, diagnostics = _validate_staged_skill(staged_dir)
        conflict = bool(name) and name in self._library.existing_skill_names(destination_scope)
        scan = _scan_staged_dir(staged_dir)
        return ImportPreview(
            staging_root=staging_root,
            staging_dir=staged_dir,
            name=name,
            description=description,
            destination_scope=destination_scope,
            conflict=conflict,
            file_count=scan.file_count,
            resource_dirs=scan.resource_dirs,
            has_scripts_or_executables=scan.has_scripts_or_executables,
            diagnostics=tuple(diagnostics),
            fingerprint=scan.fingerprint,
        )

    def preview_from_folder(self, folder: Path, *, destination_scope: InstallScope) -> ImportPreview:
        staging_root = self._new_staging_root()
        try:
            supplied = Path(folder)
            if is_link_like(supplied):
                # Checked before resolve(), which would erase the evidence.
                raise SkillImportError(f"'{supplied}' is a symlink or junction; refusing to import it")
            source = supplied.resolve()
            staged_dir = staging_root / (source.name or "skill")
            _stage_folder(source, staged_dir)
            return self._finish_preview(staging_root, staged_dir, destination_scope)
        except SkillImportError:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

    def preview_from_zip(self, zip_path: Path, *, destination_scope: InstallScope) -> ImportPreview:
        staging_root = self._new_staging_root()
        try:
            extract_dir = staging_root / "extracted"
            try:
                safe_extract_zip(Path(zip_path), extract_dir)
                staged_dir = find_skill_root(extract_dir)
            except ArchiveError as exc:
                raise SkillImportError(str(exc)) from exc
            return self._finish_preview(staging_root, staged_dir, destination_scope)
        except SkillImportError:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

    def preview_from_github(self, url: str, *, destination_scope: InstallScope) -> ImportPreview:
        staging_root = self._new_staging_root()
        try:
            try:
                target = parse_github_url(url)
                staged_dir = self._github_fetcher.fetch(target, staging_root)
            except GitHubImportError as exc:
                raise SkillImportError(str(exc)) from exc
            return self._finish_preview(staging_root, staged_dir, destination_scope)
        except SkillImportError:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

    def _reconfirm_staged(self, preview: ImportPreview) -> None:
        """Prove the staged skill is still exactly what the preview approved.

        Staging is a temp directory anyone can write to between preview and
        install. Re-deriving the name, the validity verdict, the
        script/executable finding, and the content fingerprint means mutable
        staging data cannot turn an approved preview into the installation of
        something else.
        """
        name, _description, diagnostics = _validate_staged_skill(preview.staging_dir)
        scan = _scan_staged_dir(preview.staging_dir)
        changed = (
            name != preview.name
            or any(d.is_error for d in diagnostics)
            or scan.fingerprint != preview.fingerprint
            or scan.has_scripts_or_executables != preview.has_scripts_or_executables
            or scan.file_count != preview.file_count
        )
        if changed:
            raise SkillImportError(
                "the staged skill no longer matches the preview it was approved from; "
                "re-run the preview before installing"
            )

    def install(self, preview: ImportPreview, *, replace: bool = False) -> InstalledSkillSummary:
        """Install a validated preview. Requires ``replace=True`` over an existing skill.

        The preview's ``conflict`` flag is display information only. The
        destination is re-resolved here, immediately before the filesystem is
        touched, because a skill can appear at that path between preview and
        install — installing over it on the strength of a stale "no conflict"
        reading would replace someone else's skill without ever being asked.
        """
        if not preview.ok:
            raise SkillImportError("cannot install a preview that failed validation")
        if preview.destination_scope == InstallScope.BUNDLED:
            raise SkillImportError("cannot install into the bundled scope")

        dest_root = self._library.dir_for_scope(preview.destination_scope)
        if dest_root is None:
            raise SkillImportError(f"no directory configured for scope {preview.destination_scope.value}")

        self._reconfirm_staged(preview)

        dest_root.mkdir(parents=True, exist_ok=True)
        final_dir = dest_root / preview.name
        if is_link_like(final_dir):
            raise SkillImportError(
                f"'{final_dir}' is a symlink or junction; refusing to install through it. "
                "Remove it first, then re-run the import."
            )
        # Live conflict, re-derived: the folder may have appeared since the
        # preview, or the name may now be taken by a legacy JSON skill.
        occupied = os.path.lexists(final_dir) or preview.name in self._library.existing_skill_names(
            preview.destination_scope
        )
        if occupied and not replace:
            raise SkillImportError(
                f"a skill named '{preview.name}' already exists in {preview.destination_scope.value}; "
                "explicit replacement is required"
            )

        token = uuid.uuid4().hex[:8]
        tmp_new = dest_root / f".{preview.name}.new-{token}"
        tmp_old = dest_root / f".{preview.name}.old-{token}"

        try:
            shutil.copytree(preview.staging_dir, tmp_new)
            try:
                if os.path.lexists(final_dir):
                    os.replace(final_dir, tmp_old)
                try:
                    os.replace(tmp_new, final_dir)
                except Exception:
                    if os.path.lexists(tmp_old):
                        os.replace(tmp_old, final_dir)
                    raise
            finally:
                if os.path.lexists(tmp_old):
                    shutil.rmtree(tmp_old, ignore_errors=True)
        finally:
            if os.path.lexists(tmp_new):
                shutil.rmtree(tmp_new, ignore_errors=True)
            self.cleanup(preview)

        installed_id = f"{preview.destination_scope.value}:{preview.name}"
        summary = self._library.list_installed(preview.destination_scope)
        match = next((s for s in summary if s.installed_id == installed_id), None)
        if match is None:
            raise SkillImportError("install completed but the skill could not be re-read")
        return match

    def cleanup(self, preview: ImportPreview) -> None:
        """Remove a preview's staging directory. Always safe to call more than once."""
        shutil.rmtree(preview.staging_root, ignore_errors=True)
