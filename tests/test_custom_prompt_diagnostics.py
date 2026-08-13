"""Stale saved custom-prompt diagnostics.

AppSettings.system_prompt persists across Aura versions and is appended to
the canonical production prompt (see aura/settings.py,
aura/context_gearbox/runtime.py). A prompt saved under the retired
Planner/Worker architecture can still carry that vocabulary long after the
runtime collapsed to a single production role. diagnose_custom_prompt must
surface that conservatively, without ever mutating the saved prompt.
"""
from __future__ import annotations

from aura.context_gearbox.runtime import (
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
)


def test_diagnostics_surface_retired_architecture_terms() -> None:
    custom = (
        "Use RuntimeRole and RegistryMode to pick behavior. "
        "Call update_worker_todo to update progress. Worker TODO must stay current. "
        "Planner role: draft a plan. Worker role: execute it."
    )

    diagnostics = diagnose_custom_prompt(custom)

    assert "runtimerole" in diagnostics.legacy_terms
    assert "registrymode" in diagnostics.legacy_terms
    assert "update_worker_todo" in diagnostics.legacy_terms
    assert "worker todo" in diagnostics.legacy_terms
    assert "planner role" in diagnostics.legacy_terms
    assert "worker role" in diagnostics.legacy_terms

    line = format_custom_prompt_diagnostics(diagnostics)
    assert "retired architecture terms" in line
    assert "runtimerole" in line


def test_diagnostics_are_conservative_about_ordinary_words() -> None:
    """Bare "planner"/"worker" prose must not trip the retired-terms scan."""
    custom = "Act as a careful planner and a diligent worker on this task."

    diagnostics = diagnose_custom_prompt(custom)

    assert diagnostics.legacy_terms == ()
    line = format_custom_prompt_diagnostics(diagnostics)
    assert "retired architecture terms" not in line


def test_empty_custom_prompt_has_no_legacy_terms() -> None:
    diagnostics = diagnose_custom_prompt("")

    assert diagnostics.legacy_terms == ()
    assert diagnostics.is_empty is True


def test_diagnostics_never_mutate_the_custom_prompt() -> None:
    """Diagnosing is purely observational — the saved prompt text is untouched."""
    custom = "Use RuntimeRole for behavior."

    diagnostics = diagnose_custom_prompt(custom)

    assert diagnostics.char_count == len(custom)
    assert diagnostics.appended_char_count == len(custom)
