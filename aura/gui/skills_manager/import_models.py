"""Presentation shapes for one skill import, from source choice to review.

An import has exactly two user-visible shapes: what the user picked
(:class:`ImportSource`) and what the backend made of it
(:class:`ImportPreviewView`). Both are plain data so the review dialog can
be rendered — and asserted — without a staging directory in sight.

:class:`aura.skills.importer.ImportPreview` is the authority for every
judgement here: the name, the description, the destination, the conflict
flag, the file count, the resource directories, the script finding, the
diagnostics, and whether the preview validated. Nothing in this module
re-derives any of it, and nothing carries a staging path, an installation
path, or a fingerprint — where Aura keeps skills is not part of this
surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aura.gui.skills_manager.models import scope_label
from aura.gui.skills_manager.redaction import redact_paths
from aura.skills.identity import InstallScope
from aura.skills.importer import ImportPreview

#: The destinations a user may import into. Bundled skills ship with Aura
#: and are never an import target, so the scope is not offered at all.
IMPORTABLE_SCOPES: tuple[InstallScope, ...] = (InstallScope.PROJECT, InstallScope.PERSONAL)

SCOPE_HINTS: dict[InstallScope, str] = {
    InstallScope.PROJECT: "Available in this project",
    InstallScope.PERSONAL: "Available across projects",
}

#: Shown in the review dialog. Installing a skill changes what Aura reads,
#: never what Aura is allowed to do.
IMPORT_PERMISSION_REMINDER = (
    "Aura does not run anything in this skill while importing it. Installing "
    "or using a skill grants no shell, network, file-mutation, external-read, "
    "or script-execution permission — a skill only guides how Aura works."
)

SOURCE_FOLDER = "folder"
SOURCE_ZIP = "zip"
SOURCE_GITHUB = "github"


class ImportDecision(str, Enum):
    """What the user chose in the review dialog."""

    CANCEL = "cancel"
    INSTALL = "install"
    REPLACE = "replace"


@dataclass(frozen=True)
class ImportSource:
    """One chosen import source, with the label the user may be shown.

    ``location`` is the folder, archive, or URL handed to the backend and is
    never rendered; ``label`` is the filename, folder name, or URL the user
    typed or picked, and is the only source text the GUI shows.
    """

    kind: str
    location: str
    label: str
    scope: InstallScope

    @property
    def scope_label(self) -> str:
        return scope_label(self.scope.value)


@dataclass(frozen=True)
class ImportPreviewView:
    """Everything the review dialog renders for one staged skill."""

    source_label: str
    name: str
    description: str
    destination_label: str
    destination_hint: str
    conflict: bool
    file_count: int
    resource_dirs_text: str
    has_scripts: bool
    scripts_text: str
    diagnostics: tuple[str, ...]
    installable: bool

    @property
    def decision(self) -> ImportDecision:
        """The one install action this preview offers, if it offers any."""
        if not self.installable:
            return ImportDecision.CANCEL
        return ImportDecision.REPLACE if self.conflict else ImportDecision.INSTALL


def build_preview_view(preview: ImportPreview, source: ImportSource) -> ImportPreviewView:
    """Translate a backend preview into the shape the review dialog shows."""
    scope = preview.destination_scope
    return ImportPreviewView(
        source_label=source.label,
        name=preview.name or "(no name)",
        description=redact_paths(preview.description) or "(no description)",
        destination_label=scope_label(scope.value),
        destination_hint=SCOPE_HINTS.get(scope, ""),
        conflict=bool(preview.conflict),
        file_count=int(preview.file_count),
        resource_dirs_text=", ".join(preview.resource_dirs) if preview.resource_dirs else "None",
        has_scripts=bool(preview.has_scripts_or_executables),
        scripts_text=(
            "Yes — this skill contains scripts or executable-looking files."
            if preview.has_scripts_or_executables
            else "No"
        ),
        diagnostics=tuple(
            f"{diagnostic.severity.value}: {diagnostic.code} — {redact_paths(diagnostic.message)}"
            for diagnostic in preview.diagnostics
        ),
        installable=bool(preview.ok),
    )


__all__ = [
    "IMPORTABLE_SCOPES",
    "IMPORT_PERMISSION_REMINDER",
    "SCOPE_HINTS",
    "SOURCE_FOLDER",
    "SOURCE_GITHUB",
    "SOURCE_ZIP",
    "ImportDecision",
    "ImportPreviewView",
    "ImportSource",
    "build_preview_view",
]
