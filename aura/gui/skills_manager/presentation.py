"""Turning SkillLibrary's answers into what the manager window renders.

Row status, the reason a row is or is not in play, and everything the detail
pane shows are built here from :class:`~aura.skills.library` results. Every
judgement behind them — validity, enabled state, precedence, shadowing,
workspace applicability — was already made by SkillLibrary; this module only
puts it into the user's words, and drops any absolute path on the way out.
"""
from __future__ import annotations

from aura.gui.skills_manager.models import SkillDetail, SkillRow, scope_label
from aura.gui.skills_manager.redaction import redact_paths
from aura.skills.identity import InstalledSkillId, InstallScope
from aura.skills.library import InstalledSkillSummary, SkillInspection

#: Stands in for a description Aura could not read, so an unreadable entry
#: still says something useful in the list.
INVALID_DESCRIPTION = "This skill could not be read. Open it for the details."


def build_row(
    summary: InstalledSkillSummary,
    effective_ids: set[str],
    selected_ids: set[str],
) -> SkillRow:
    usable = summary.installed_id in effective_ids
    description = summary.description or ("" if summary.valid else INVALID_DESCRIPTION)
    return SkillRow(
        install_id=summary.installed_id,
        scope=summary.scope.value,
        name=summary.name,
        description=redact_paths(description),
        status_text=_status_text(summary, usable),
        enabled=summary.enabled,
        valid=summary.valid,
        usable=usable,
        already_selected=summary.installed_id in selected_ids,
        can_uninstall=summary.scope != InstallScope.BUNDLED,
    )


def _status_text(summary: InstalledSkillSummary, usable: bool) -> str:
    """Say, in the user's terms, why a row is or is not in play."""
    if not summary.valid:
        return "Invalid"
    if not summary.enabled:
        return "Disabled"
    if summary.shadowed_by:
        winner = InstalledSkillId.parse(summary.shadowed_by)
        if winner is not None:
            return f"Shadowed by the {scope_label(winner.scope.value).lower()} skill"
        return "Shadowed"
    if not usable:
        return "Not available in this workspace"
    return "Enabled"


def build_detail(row: SkillRow, inspection: SkillInspection | None) -> SkillDetail:
    description = row.description
    fields: list[tuple[str, str]] = []
    diagnostics: tuple[str, ...] = ()

    if inspection is not None:
        if inspection.description:
            description = inspection.description
        if inspection.model:
            fields.append(("Model", inspection.model))
        if inspection.task_kinds:
            fields.append(("Task kinds", ", ".join(inspection.task_kinds)))
        if inspection.path_globs:
            fields.append(("Paths", ", ".join(inspection.path_globs)))
        if inspection.triggers:
            fields.append(("Triggers", ", ".join(inspection.triggers)))
        if inspection.resource_entries:
            fields.append(("Resources", ", ".join(inspection.resource_entries)))
        elif inspection.has_resources:
            fields.append(("Resources", "included with this skill"))
        if inspection.body_chars:
            fields.append(("Instructions", f"{inspection.body_chars:,} characters"))
        diagnostics = tuple(
            f"{diagnostic.severity.value}: {diagnostic.code} — {redact_paths(diagnostic.message)}"
            for diagnostic in inspection.diagnostics
        )

    return SkillDetail(
        install_id=row.install_id,
        name=row.name,
        scope_label=row.scope_label,
        status_text=row.status_text,
        description=redact_paths(description),
        fields=tuple((label, redact_paths(value)) for label, value in fields),
        diagnostics=diagnostics,
    )


__all__ = ["INVALID_DESCRIPTION", "build_detail", "build_row"]
