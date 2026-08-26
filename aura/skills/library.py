"""SkillLibrary — the sole owner of installed-skill discovery and lifecycle.

Discovers project, personal, and bundled SKILL.md folders (plus legacy flat
JSON authored skills), applies duplicate-name precedence (project > personal
> bundled), and is the one place enable/disable, uninstall, and resource
resolution happen. Graduated hazards and reflection-refined guards are
internal runtime guards, not installable user skills, and are never touched
here — :func:`aura.skills.reader.read_skills` appends them separately.

Runtime discovery and management inventory answer two different questions.
:meth:`SkillLibrary.discover_effective_skills` returns only skills that are
valid and enabled, because that set feeds prompt composition. The management
surface (:meth:`SkillLibrary.list_installed`, :meth:`SkillLibrary.inspect`)
additionally reports entries that failed to load, so a broken skill is a
visible, fixable row rather than a folder that silently disappeared.

Nothing here prints or opens a dialog; every operation returns structured
data for a caller (a CLI, a future GUI, or a test) to act on.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from aura.paths import data_dir, is_link_like
from aura.skills.description import derive_skill_description
from aura.skills.diagnostics import SkillDiagnostic, error, warning
from aura.skills.frontmatter import parse_skill_markdown
from aura.skills.identity import InstalledSkillId, InstallScope
from aura.skills.legacy_json import LegacySkillEntry, read_legacy_json_skills
from aura.skills.manifest import SkillManifest
from aura.skills.models import Skill, SkillProvenance
from aura.skills.resources import SkillResourceError, resolve_skill_resource

logger = logging.getLogger(__name__)

_RESOURCE_DIR_NAMES = ("scripts", "references", "assets")


def _bundled_skills_dir() -> Path | None:
    """Resolve the packaged, read-only skill directory in dev and packaged builds."""
    local_dir = Path(__file__).resolve().parent / "bundled"
    if local_dir.is_dir():
        return local_dir
    try:
        from aura.resources import get_resource_path

        resource_dir = get_resource_path(Path("aura") / "skills" / "bundled")
    except Exception:
        logger.debug("Failed to resolve packaged skill directory", exc_info=True)
        return None
    return resource_dir if resource_dir.is_dir() else None


def _dir_has_resources(directory: Path) -> bool:
    """True when a skill directory carries anything besides SKILL.md.

    Standard nested ``scripts/``, ``references/``, and ``assets/``
    directories count, even empty — their presence is the signal. Any other
    flat file next to SKILL.md counts too, for backward compatibility.
    """
    try:
        for entry in directory.iterdir():
            if entry.name == "SKILL.md":
                continue
            if entry.is_dir() and entry.name in _RESOURCE_DIR_NAMES:
                return True
            if entry.is_file():
                return True
        return False
    except OSError:
        return False


def _workspace_marker_present(workspace_root: str | Path, marker: str) -> bool:
    candidate = str(marker or "").strip()
    if not candidate:
        return False
    try:
        return (Path(workspace_root) / candidate).exists()
    except OSError:
        return False


def _skill_applies_to_workspace(skill: Skill, workspace_root: str | Path) -> bool:
    if not skill.workspace_markers:
        return True
    return any(_workspace_marker_present(workspace_root, marker) for marker in skill.workspace_markers)


@dataclass(frozen=True)
class InstalledSkillSummary:
    """One row of :meth:`SkillLibrary.list_installed`.

    ``valid=False`` marks an entry that exists on disk under a stable
    ``scope:name`` identity but could not be loaded. Such a row carries its
    ``diagnostics`` and is addressable for replacement or uninstall, but it
    is never part of the effective runtime skill set.
    """

    installed_id: str
    scope: InstallScope
    name: str
    description: str
    enabled: bool
    has_resources: bool
    source_dir: Path | None
    shadowed_by: str | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    valid: bool = True


@dataclass(frozen=True)
class SkillInspection:
    """Full detail for one installed skill (:meth:`SkillLibrary.inspect`).

    Returned for broken entries too — with ``valid=False``, empty metadata,
    and the diagnostics explaining the failure — so a management caller can
    show *why* an addressable skill does not load.
    """

    installed_id: str
    scope: InstallScope
    name: str
    description: str
    body_chars: int
    task_kinds: tuple[str, ...]
    path_globs: tuple[str, ...]
    triggers: tuple[str, ...]
    model: str | None
    has_resources: bool
    resource_entries: tuple[str, ...]
    source_dir: Path | None
    enabled: bool
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    valid: bool = True


@dataclass(frozen=True)
class _InvalidEntry:
    """One addressable installed entry that exists but could not be loaded."""

    name: str
    path: Path
    is_folder: bool
    diagnostics: tuple[SkillDiagnostic, ...]


@dataclass(frozen=True)
class _ScopeScan:
    """Everything one scope's directory yielded in a single pass."""

    skills: tuple[Skill, ...] = ()
    invalid: tuple[_InvalidEntry, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    #: Per-skill diagnostics keyed by bare skill name, so a summary carries
    #: exactly its own findings instead of matching diagnostics by path prefix.
    skill_diagnostics: dict[str, tuple[SkillDiagnostic, ...]] = field(default_factory=dict)


def _bare_name(skill: Skill) -> str:
    for key, value in skill.origin:
        if key == "skill_id" and str(value).strip():
            return str(value).strip()
    return ""


def _addressable(name: str) -> bool:
    """True when *name* can safely become half of a ``scope:name`` identity.

    A lifecycle identity never carries a separator or an arbitrary path
    fragment — an entry whose on-disk name cannot round-trip through
    :class:`aura.skills.identity.InstalledSkillId` stays a diagnostic only,
    never an addressable row.
    """
    text = str(name or "").strip()
    if not text or text in (".", ".."):
        return False
    return ":" not in text and "/" not in text and "\\" not in text


class SkillLibrary:
    """Discovers and manages project/personal/bundled installed skills.

    Directory resolution defaults to Aura's real config/data locations;
    ``project_dir``/``personal_dir``/``bundled_dir`` overrides exist purely
    for test isolation so a test never has to touch the developer's real
    personal-skill directory.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        project_dir: Path | None = None,
        personal_dir: Path | None = None,
        bundled_dir: Path | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._project_dir = project_dir or (self._workspace_root / ".aura" / "skills" / "authored")
        self._personal_dir = personal_dir or (data_dir() / "skills" / "authored")
        self._bundled_dir = bundled_dir if bundled_dir is not None else _bundled_skills_dir()
        # Personal and bundled state share one store, keyed off personal_dir's
        # parent — so a test that overrides personal_dir also isolates bundled
        # enable/disable state from the developer's real data_dir().
        self._manifest = SkillManifest(self._workspace_root, personal_state_dir=self._personal_dir.parent)

    # ---- directory resolution ----------------------------------------------

    def dir_for_scope(self, scope: InstallScope) -> Path | None:
        if scope == InstallScope.PROJECT:
            return self._project_dir
        if scope == InstallScope.PERSONAL:
            return self._personal_dir
        return self._bundled_dir

    # ---- discovery ----------------------------------------------------------

    def _scan_scope(self, scope: InstallScope) -> _ScopeScan:
        """Read one scope's directory once, classifying every entry it holds.

        A skill directory that is itself a symlink or junction is never read:
        it is reported diagnostically and excluded, because its content lives
        outside the scope directory the user actually installed into. The
        same refusal applies to the scope directory itself, checked before
        anything can resolve that evidence away.
        """
        directory = self.dir_for_scope(scope)
        if directory is None or not directory.is_dir():
            return _ScopeScan()
        if is_link_like(directory):
            return _ScopeScan(
                diagnostics=(
                    error(
                        "linked_skill_root",
                        f"{scope.value} skill directory is a symlink or junction; no skills are read from it",
                        str(directory),
                    ),
                )
            )

        skills: list[Skill] = []
        invalid: list[_InvalidEntry] = []
        diagnostics: list[SkillDiagnostic] = []
        skill_diagnostics: dict[str, tuple[SkillDiagnostic, ...]] = {}
        provenance = SkillProvenance.BUNDLED if scope is InstallScope.BUNDLED else SkillProvenance.USER_AUTHORED
        seen_names: set[str] = set()

        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            name = entry.name
            if is_link_like(entry):
                diagnostics.append(
                    self._record_link_entry(
                        invalid,
                        seen_names,
                        name,
                        entry,
                        "linked_skill_directory",
                        "installed skill entry is a symlink or junction; it is excluded from discovery",
                        str(entry),
                    )
                )
                continue
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if is_link_like(skill_md):
                diagnostics.append(
                    self._record_link_entry(
                        invalid,
                        seen_names,
                        name,
                        entry,
                        "linked_skill_manifest",
                        "SKILL.md is a symlink or junction; the skill is excluded from discovery",
                        str(skill_md),
                    )
                )
                continue
            if not skill_md.is_file():
                continue
            if name in seen_names:
                diagnostics.append(
                    error("duplicate_identity", f"duplicate skill name '{name}' in {scope.value}", str(entry))
                )
                continue
            skill, entry_diagnostics = self._build_markdown_skill(entry, skill_md, scope, provenance, name)
            diagnostics.extend(entry_diagnostics)
            if skill is not None:
                seen_names.add(name)
                skills.append(skill)
                skill_diagnostics[name] = tuple(entry_diagnostics)
            elif _addressable(name):
                # A folder whose SKILL.md exists but does not load keeps its
                # identity: it stays listable, replaceable, and removable.
                seen_names.add(name)
                invalid.append(
                    _InvalidEntry(name=name, path=entry, is_folder=True, diagnostics=tuple(entry_diagnostics))
                )

        legacy_entries, legacy_invalid, legacy_diagnostics = read_legacy_json_skills(directory)
        diagnostics.extend(legacy_diagnostics)
        for legacy in legacy_entries:
            if legacy.name in seen_names:
                diagnostics.append(
                    error("duplicate_identity", f"duplicate skill name '{legacy.name}' in {scope.value}", legacy.name)
                )
                continue
            seen_names.add(legacy.name)
            skills.append(self._build_legacy_skill(legacy, scope, provenance))
        for broken in legacy_invalid:
            if broken.name in seen_names:
                continue
            seen_names.add(broken.name)
            invalid.append(
                _InvalidEntry(name=broken.name, path=broken.path, is_folder=False, diagnostics=broken.diagnostics)
            )

        return _ScopeScan(
            skills=tuple(skills),
            invalid=tuple(invalid),
            diagnostics=tuple(diagnostics),
            skill_diagnostics=skill_diagnostics,
        )

    @staticmethod
    def _record_link_entry(
        invalid: list[_InvalidEntry],
        seen_names: set[str],
        name: str,
        entry: Path,
        code: str,
        message: str,
        path: str,
    ) -> SkillDiagnostic:
        """Diagnose one linked entry and keep it addressable for cleanup."""
        diagnostic = error(code, message, path)
        if _addressable(name) and name not in seen_names:
            seen_names.add(name)
            invalid.append(_InvalidEntry(name=name, path=entry, is_folder=True, diagnostics=(diagnostic,)))
        return diagnostic

    def _discover_scope(self, scope: InstallScope) -> tuple[list[Skill], list[SkillDiagnostic]]:
        """Valid skills and every finding for one scope (runtime discovery view)."""
        scan = self._scan_scope(scope)
        return list(scan.skills), list(scan.diagnostics)

    def _build_markdown_skill(
        self,
        directory: Path,
        skill_md: Path,
        scope: InstallScope,
        provenance: SkillProvenance,
        name: str,
    ) -> tuple[Skill | None, list[SkillDiagnostic]]:
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            return None, [error("unreadable", str(exc), str(skill_md))]

        parsed = parse_skill_markdown(raw, source=str(skill_md))
        diagnostics = list(parsed.diagnostics)
        if not parsed.ok:
            return None, diagnostics

        declared_name = parsed.metadata.get("name")
        if declared_name and declared_name != name:
            diagnostics.append(
                warning(
                    "name_mismatch",
                    f"declared name '{declared_name}' does not match folder name '{name}'",
                    str(skill_md),
                )
            )

        installed_id = InstalledSkillId(scope=scope, name=name)
        skill = Skill(
            text=parsed.body,
            task_kinds=tuple(parsed.metadata.get("task_kinds", [])),
            path_globs=tuple(parsed.metadata.get("path_globs", [])),
            model=parsed.metadata.get("model"),
            provenance=provenance,
            origin=(("skill_id", name),),
            triggers=tuple(parsed.metadata.get("triggers", [])),
            workspace_markers=tuple(parsed.metadata.get("workspace_markers", [])),
            description=derive_skill_description(parsed.body, explicit=parsed.metadata.get("description")),
            has_resources=_dir_has_resources(directory),
            install_id=str(installed_id),
            source_dir=directory,
        )
        return skill, diagnostics

    def _build_legacy_skill(self, entry: LegacySkillEntry, scope: InstallScope, provenance: SkillProvenance) -> Skill:
        installed_id = InstalledSkillId(scope=scope, name=entry.name)
        origin = entry.origin or (("skill_id", entry.name),)
        return Skill(
            text=entry.text,
            task_kinds=entry.task_kinds,
            path_globs=entry.path_globs,
            model=entry.model,
            provenance=provenance,
            origin=origin,
            triggers=entry.triggers,
            description=derive_skill_description(entry.text, explicit=entry.description),
            has_resources=False,
            install_id=str(installed_id),
            source_dir=None,
        )

    def discover_effective_skills(self) -> tuple[list[Skill], list[SkillDiagnostic]]:
        """Every installed skill this workspace actually loads, in read order.

        Applies duplicate-name precedence (project, then personal, then
        bundled), enable/disable state, and — bundled only, matching prior
        behavior — workspace-marker gating. Invalid entries never appear here
        (that is what :meth:`list_installed` is for), but their diagnostics
        do. Never propagates exceptions: degrades to ``([], [])`` on
        unexpected failure, same silent-degrade contract as the rest of the
        skills package.
        """
        try:
            all_diagnostics: list[SkillDiagnostic] = []
            by_scope: dict[InstallScope, dict[str, Skill]] = {}
            for scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
                scan = self._scan_scope(scope)
                all_diagnostics.extend(scan.diagnostics)
                by_scope[scope] = {_bare_name(s): s for s in scan.skills}

            chosen: dict[str, Skill] = {}
            for scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
                for name, skill in by_scope[scope].items():
                    chosen.setdefault(name, skill)

            effective: list[Skill] = []
            for skill in chosen.values():
                installed_id = InstalledSkillId.parse(skill.install_id or "")
                if installed_id is not None and self._manifest.is_disabled(installed_id):
                    continue
                if skill.provenance == SkillProvenance.BUNDLED and not _skill_applies_to_workspace(
                    skill, self._workspace_root
                ):
                    continue
                effective.append(skill)

            return effective, all_diagnostics
        except Exception:
            logger.debug("discover_effective_skills failed", exc_info=True)
            return [], []

    # ---- lifecycle: listing / inspection ------------------------------------

    def list_installed(self, scope: InstallScope | None = None) -> list[InstalledSkillSummary]:
        """Installed skill inventory, optionally filtered to one scope.

        Every discovered entry is listed, including ones shadowed by a
        higher-precedence scope's same-named skill (``shadowed_by`` names the
        winner) and ones that failed to load (``valid=False``, with their
        diagnostics attached) — this is the raw inventory a GUI would show,
        not the effective runtime set (:meth:`discover_effective_skills`).
        A broken entry belongs here precisely so it can be seen, replaced, or
        uninstalled instead of silently vanishing.
        """
        scopes = (scope,) if scope is not None else (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED)
        scans: dict[InstallScope, _ScopeScan] = {
            one_scope: self._scan_scope(one_scope)
            for one_scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED)
        }

        # Only a skill that actually loads can shadow another one.
        winner_by_name: dict[str, InstallScope] = {}
        for one_scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
            for skill in scans[one_scope].skills:
                winner_by_name.setdefault(_bare_name(skill), one_scope)

        summaries: list[InstalledSkillSummary] = []
        for one_scope in scopes:
            scan = scans[one_scope]
            for skill in scan.skills:
                name = _bare_name(skill)
                installed_id = InstalledSkillId(scope=one_scope, name=name)
                winner = winner_by_name.get(name)
                summaries.append(
                    InstalledSkillSummary(
                        installed_id=str(installed_id),
                        scope=one_scope,
                        name=name,
                        description=skill.description or "",
                        enabled=not self._manifest.is_disabled(installed_id),
                        has_resources=skill.has_resources,
                        source_dir=skill.source_dir,
                        shadowed_by=str(InstalledSkillId(scope=winner, name=name)) if winner != one_scope else None,
                        diagnostics=scan.skill_diagnostics.get(name, ()),
                        valid=True,
                    )
                )
            for broken in scan.invalid:
                installed_id = InstalledSkillId(scope=one_scope, name=broken.name)
                winner = winner_by_name.get(broken.name)
                summaries.append(
                    InstalledSkillSummary(
                        installed_id=str(installed_id),
                        scope=one_scope,
                        name=broken.name,
                        description="",
                        enabled=not self._manifest.is_disabled(installed_id),
                        has_resources=False,
                        source_dir=broken.path if broken.is_folder else None,
                        shadowed_by=(
                            str(InstalledSkillId(scope=winner, name=broken.name)) if winner is not None else None
                        ),
                        diagnostics=broken.diagnostics,
                        valid=False,
                    )
                )
        return summaries

    def _find_skill(self, installed_id: str) -> Skill | None:
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            return None
        for skill in self._scan_scope(parsed.scope).skills:
            if skill.install_id == installed_id:
                return skill
        return None

    def inspect(self, installed_id: str) -> SkillInspection | None:
        """Full detail for one installed skill, or None if it does not exist.

        A broken-but-addressable entry is *not* None: it comes back with
        ``valid=False`` and its diagnostics, so a caller can explain the
        failure instead of reporting the skill as missing.
        """
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            return None
        scan = self._scan_scope(parsed.scope)
        skill = next((s for s in scan.skills if s.install_id == installed_id), None)
        enabled = not self._manifest.is_disabled(parsed)
        if skill is None:
            broken = next((entry for entry in scan.invalid if entry.name == parsed.name), None)
            if broken is None:
                return None
            return SkillInspection(
                installed_id=installed_id,
                scope=parsed.scope,
                name=parsed.name,
                description="",
                body_chars=0,
                task_kinds=(),
                path_globs=(),
                triggers=(),
                model=None,
                has_resources=False,
                resource_entries=(),
                source_dir=broken.path if broken.is_folder else None,
                enabled=enabled,
                diagnostics=broken.diagnostics,
                valid=False,
            )

        resource_entries: tuple[str, ...] = ()
        if skill.source_dir is not None and skill.source_dir.is_dir():
            resource_entries = tuple(
                sorted(entry.name for entry in skill.source_dir.iterdir() if entry.name != "SKILL.md")
            )
        return SkillInspection(
            installed_id=installed_id,
            scope=parsed.scope,
            name=parsed.name,
            description=skill.description or "",
            body_chars=len(skill.text),
            task_kinds=skill.task_kinds,
            path_globs=skill.path_globs,
            triggers=skill.triggers,
            model=skill.model,
            has_resources=skill.has_resources,
            resource_entries=resource_entries,
            source_dir=skill.source_dir,
            enabled=enabled,
            diagnostics=scan.skill_diagnostics.get(parsed.name, ()),
            valid=True,
        )

    # ---- lifecycle: enable / disable / uninstall -----------------------------

    def set_enabled(self, installed_id: str, enabled: bool) -> None:
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            raise ValueError(f"'{installed_id}' is not a valid installed skill id")
        self._manifest.set_enabled(parsed, enabled)

    def uninstall(self, installed_id: str) -> None:
        """Delete a project/personal skill entry. Bundled skills cannot be deleted.

        Works for a broken entry too — a malformed skill must be removable,
        not stuck on disk because it no longer parses. The target is always
        re-resolved from a fresh scan of the scope directory, never taken
        from caller input, so only something this library discovered directly
        inside that scope can be deleted.
        """
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            raise ValueError(f"'{installed_id}' is not a valid installed skill id")
        if parsed.scope == InstallScope.BUNDLED:
            raise ValueError("bundled skills cannot be deleted, only disabled")
        scope_dir = self.dir_for_scope(parsed.scope)
        if scope_dir is None:
            raise ValueError(f"no directory configured for scope {parsed.scope.value}")

        scan = self._scan_scope(parsed.scope)
        skill = next((s for s in scan.skills if s.install_id == installed_id), None)
        if skill is not None and skill.source_dir is not None:
            target = skill.source_dir
        else:
            broken = next((entry for entry in scan.invalid if entry.name == parsed.name), None)
            if broken is None:
                raise ValueError(f"'{installed_id}' is not an installed folder skill")
            target = broken.path

        _remove_installed_target(target, scope_dir)
        self._manifest.forget(parsed)

    # ---- resource resolution --------------------------------------------------

    def resolve_resource(self, installed_id: str, relative_path: str) -> Path:
        """Resolve *relative_path* inside one installed skill's directory."""
        skill = self._find_skill(installed_id)
        if skill is None or skill.source_dir is None:
            raise SkillResourceError(f"'{installed_id}' is not an installed folder skill")
        return resolve_skill_resource(skill.source_dir, relative_path)

    # ---- import plumbing (used by aura.skills.importer) -----------------------

    def existing_skill_names(self, scope: InstallScope) -> set[str]:
        """Every name already taken in *scope*, whether it loads or not.

        A broken entry still occupies its folder, so it still conflicts — an
        import over it is a replacement, not a fresh install.
        """
        scan = self._scan_scope(scope)
        return {_bare_name(s) for s in scan.skills} | {entry.name for entry in scan.invalid}


def _remove_installed_target(target: Path, scope_dir: Path) -> None:
    """Delete one installed entry, refusing to recurse through a link.

    A symlinked or junctioned skill directory is unlinked, never walked:
    ``shutil.rmtree`` through a Windows junction deletes the *target's*
    contents, which is exactly the escape a planted junction is for. The
    containment assertion is cheap insurance that the path came from a
    library scan of *scope_dir* and not from somewhere else.
    """
    if Path(target).parent != Path(scope_dir):
        raise ValueError(f"'{target}' is not directly inside {scope_dir}")
    if is_link_like(target):
        try:
            os.rmdir(target)  # directory symlink or junction: removes the link only
        except OSError:
            Path(target).unlink()  # file symlink
        return
    if Path(target).is_dir():
        shutil.rmtree(target)
        return
    Path(target).unlink()
