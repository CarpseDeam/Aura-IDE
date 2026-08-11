from __future__ import annotations

import logging
from pathlib import Path


def compute_dependents(
    workspace_root: Path, files: list[str], force_graph: bool = False
) -> list[str]:
    """Return sorted list of files that depend on the given files, or [] on failure."""
    if not files:
        return []

    try:
        from aura.code_intel.index import CodeIntelIndex

        index = CodeIntelIndex(workspace_root)
        # Full refresh first for blast-radius context
        index.refresh()
        # Re-parse the specific files
        index.refresh(changed_files=files)
        dependents: set[str] = set()

        for path_str in files:
            p = Path(path_str)
            try:
                normalized = (
                    p.relative_to(workspace_root).as_posix()
                    if p.is_absolute()
                    else p.as_posix()
                )
            except ValueError:
                continue
            try:
                for dep in index.get_blast_radius(normalized):
                    dependents.add(dep)
            except Exception:
                continue

        # Normalise edited files for subtraction
        edited_norm: set[str] = set()
        for path_str in files:
            p = Path(path_str)
            try:
                norm = (
                    p.relative_to(workspace_root).as_posix()
                    if p.is_absolute()
                    else p.as_posix()
                )
            except ValueError:
                continue
            edited_norm.add(norm)

        dependents -= edited_norm

        if not dependents:
            return []

        return sorted(dependents)
    except Exception:
        logging.getLogger(__name__).warning(
            "Dependency-graph annotation failed, returning empty",
            exc_info=True,
        )
        return []


def build_dependency_stanza(workspace_root: Path, files: list[str]) -> str:
    """Return a formatted ``Downstream Dependents`` stanza string, or ``""``.

    Never raises -- a graph hiccup must not disrupt a dispatch.
    """
    sorted_deps = compute_dependents(workspace_root, files)
    if not sorted_deps:
        return ""

    stanza = (
        "\n\n## Downstream Dependents (harness-computed dependency context)\n"
        "The following files import the modules being edited or reference symbols defined in them. "
        "Their public signatures, exported names, and import paths must be preserved. "
        "If a contract change is truly unavoidable, call it out explicitly rather than making it silently.\n\n"
    )
    for dep_path in sorted_deps[:15]:
        stanza += f"- {dep_path}\n"
    if len(sorted_deps) > 15:
        stanza += f"\n({len(sorted_deps) - 15} additional dependents omitted)"
    return stanza


def build_dependent_planner_notice(
    workspace_root: Path, files: list[str], force_graph: bool = False
) -> str:
    """Return a planner notice about downstream dependents, or ``""``."""
    sorted_deps = compute_dependents(workspace_root, files, force_graph=force_graph)
    if not sorted_deps:
        return ""

    lines: list[str] = [
        "Planner dependency context:\n",
        "The files just modified are depended on by:\n",
    ]
    for dep_path in sorted_deps[:15]:
        lines.append(f"- {dep_path}\n")
    if len(sorted_deps) > 15:
        lines.append(f"\n({len(sorted_deps) - 15} additional dependents omitted)\n")
    lines.append(
        "\n"
        "These files import, reference, or re-export symbols from the changed\n"
        "modules. When planning your next step, account for whether any of these\n"
        "dependents rely on a signature, name, or contract that changed. This is\n"
        "context for scoping, not a signal to act — do not redispatch because of\n"
        "this notice unless the user asks for more."
    )
    return "".join(lines)
