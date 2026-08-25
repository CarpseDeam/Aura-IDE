"""SkillLibrary — the sole owner of installed-skill discovery and lifecycle.

Discovers project, personal, and bundled SKILL.md folders (plus legacy flat
JSON authored skills), applies duplicate-name precedence (project > personal
> bundled), and is the one place enable/disable, uninstall, and resource
resolution happen. Graduated hazards and reflection-refined guards are
internal runtime guards, not installable user skills, and are never touched
here — :func:`aura.skills.reader.read_skills` appends them separately.

Nothing here prints or opens a dialog; every operation returns structured
data for a caller (a CLI, a future GUI, or a test) to act on.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from aura.paths import data_dir
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
    """One row of :meth:`SkillLibrary.list_installed`."""

    installed_id: str
    scope: InstallScope
    name: str
    description: str
    enabled: bool
    has_resources: bool
    source_dir: Path | None
    shadowed_by: str | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()


@dataclass(frozen=True)
class SkillInspection:
    """Full detail for one installed skill (:meth:`SkillLibrary.inspect`)."""

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


def _bare_name(skill: Skill) -> str:
    for key, value in skill.origin:
        if key == "skill_id" and str(value).strip():
            return str(value).strip()
    return ""


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

    def _discover_scope(self, scope: InstallScope) -> tuple[list[Skill], list[SkillDiagnostic]]:
        directory = self.dir_for_scope(scope)
        skills: list[Skill] = []
        diagnostics: list[SkillDiagnostic] = []
        if directory is None or not directory.is_dir():
            return skills, diagnostics

        provenance = SkillProvenance.BUNDLED if scope is InstallScope.BUNDLED else SkillProvenance.USER_AUTHORED
        seen_names: set[str] = set()

        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            name = entry.name
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

        legacy_entries, legacy_diagnostics = read_legacy_json_skills(directory)
        diagnostics.extend(legacy_diagnostics)
        for legacy in legacy_entries:
            if legacy.name in seen_names:
                diagnostics.append(
                    error("duplicate_identity", f"duplicate skill name '{legacy.name}' in {scope.value}", legacy.name)
                )
                continue
            seen_names.add(legacy.name)
            skills.append(self._build_legacy_skill(legacy, scope, provenance))

        return skills, diagnostics

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
        behavior — workspace-marker gating. Never propagates exceptions:
        degrades to ``([], [])`` on unexpected failure, same silent-degrade
        contract as the rest of the skills package.
        """
        try:
            all_diagnostics: list[SkillDiagnostic] = []
            by_scope: dict[InstallScope, dict[str, Skill]] = {}
            for scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
                skills, diagnostics = self._discover_scope(scope)
                all_diagnostics.extend(diagnostics)
                by_scope[scope] = {_bare_name(s): s for s in skills}

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
        """Installed skill summaries, optionally filtered to one scope.

        Every discovered skill is listed, including ones shadowed by a
        higher-precedence scope's same-named skill (``shadowed_by`` names
        the winner) — this is the raw inventory a GUI would show, not the
        effective runtime set (:meth:`discover_effective_skills`).
        """
        scopes = (scope,) if scope is not None else (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED)
        by_scope: dict[InstallScope, dict[str, Skill]] = {}
        diagnostics_by_scope: dict[InstallScope, list[SkillDiagnostic]] = {}
        for one_scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
            skills, diagnostics = self._discover_scope(one_scope)
            by_scope[one_scope] = {_bare_name(s): s for s in skills}
            diagnostics_by_scope[one_scope] = diagnostics

        winner_by_name: dict[str, InstallScope] = {}
        for one_scope in (InstallScope.PROJECT, InstallScope.PERSONAL, InstallScope.BUNDLED):
            for name in by_scope[one_scope]:
                winner_by_name.setdefault(name, one_scope)

        summaries: list[InstalledSkillSummary] = []
        for one_scope in scopes:
            for name, skill in by_scope[one_scope].items():
                installed_id = InstalledSkillId(scope=one_scope, name=name)
                winner = winner_by_name.get(name)
                shadowed_by = str(InstalledSkillId(scope=winner, name=name)) if winner != one_scope else None
                own_diagnostics = tuple(d for d in diagnostics_by_scope[one_scope] if d.path.startswith(str(skill.source_dir or "")))
                summaries.append(
                    InstalledSkillSummary(
                        installed_id=str(installed_id),
                        scope=one_scope,
                        name=name,
                        description=skill.description or "",
                        enabled=not self._manifest.is_disabled(installed_id),
                        has_resources=skill.has_resources,
                        source_dir=skill.source_dir,
                        shadowed_by=shadowed_by,
                        diagnostics=own_diagnostics,
                    )
                )
        return summaries

    def _find_skill(self, installed_id: str) -> Skill | None:
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            return None
        skills, _diagnostics = self._discover_scope(parsed.scope)
        for skill in skills:
            if skill.install_id == installed_id:
                return skill
        return None

    def inspect(self, installed_id: str) -> SkillInspection | None:
        """Full detail for one installed skill, or None if it does not exist."""
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            return None
        skills, diagnostics = self._discover_scope(parsed.scope)
        skill = next((s for s in skills if s.install_id == installed_id), None)
        if skill is None:
            return None

        resource_entries: tuple[str, ...] = ()
        if skill.source_dir is not None and skill.source_dir.is_dir():
            resource_entries = tuple(
                sorted(entry.name for entry in skill.source_dir.iterdir() if entry.name != "SKILL.md")
            )
        own_diagnostics = tuple(
            d for d in diagnostics if skill.source_dir is not None and d.path.startswith(str(skill.source_dir))
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
            enabled=not self._manifest.is_disabled(parsed),
            diagnostics=own_diagnostics,
        )

    # ---- lifecycle: enable / disable / uninstall -----------------------------

    def set_enabled(self, installed_id: str, enabled: bool) -> None:
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            raise ValueError(f"'{installed_id}' is not a valid installed skill id")
        self._manifest.set_enabled(parsed, enabled)

    def uninstall(self, installed_id: str) -> None:
        """Delete a project/personal skill folder. Bundled skills cannot be deleted."""
        parsed = InstalledSkillId.parse(installed_id)
        if parsed is None:
            raise ValueError(f"'{installed_id}' is not a valid installed skill id")
        if parsed.scope == InstallScope.BUNDLED:
            raise ValueError("bundled skills cannot be deleted, only disabled")
        skill = self._find_skill(installed_id)
        if skill is None or skill.source_dir is None:
            raise ValueError(f"'{installed_id}' is not an installed folder skill")
        shutil.rmtree(skill.source_dir)
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
        skills, _diagnostics = self._discover_scope(scope)
        return {_bare_name(s) for s in skills}
