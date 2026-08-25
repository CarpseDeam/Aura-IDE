"""Structured diagnostics for skill discovery, validation, and import.

Library and import APIs return these instead of silently dropping invalid
skills, so a later GUI can render exactly what was wrong and where. Production
prompt composition (:func:`aura.skills.reader.read_skills`) still fails
closed on an invalid skill — it just also records the diagnostic instead of
swallowing the failure silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class SkillDiagnostic:
    """One discovery/validation/import finding about a candidate skill."""

    severity: DiagnosticSeverity
    code: str
    message: str
    path: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == DiagnosticSeverity.ERROR


def error(code: str, message: str, path: str = "") -> SkillDiagnostic:
    return SkillDiagnostic(severity=DiagnosticSeverity.ERROR, code=code, message=message, path=path)


def warning(code: str, message: str, path: str = "") -> SkillDiagnostic:
    return SkillDiagnostic(severity=DiagnosticSeverity.WARNING, code=code, message=message, path=path)
