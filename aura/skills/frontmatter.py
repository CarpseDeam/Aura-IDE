"""Safe YAML front matter parsing for ``SKILL.md`` files.

Replaces the previous hand-written JSON-ish front-matter parser. Front matter
is parsed with ``yaml.safe_load`` — it can only construct plain Python
scalars, lists, and dicts, never arbitrary objects — and malformed input
produces a structured diagnostic instead of being silently dropped.

Supported fields:

* ``name``, ``description`` — the standard SKILL.md fields.
* ``task_kinds``, ``path_globs``, ``model``, ``triggers``,
  ``workspace_markers`` — Aura's existing selection metadata.

Any other declared key is ignored (forward-compatible), not an error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from aura.skills.diagnostics import SkillDiagnostic, error, warning

_KNOWN_LIST_FIELDS = ("task_kinds", "path_globs", "triggers", "workspace_markers")
_KNOWN_STRING_FIELDS = ("name", "description", "model")
_DELIMITER = "---"


@dataclass(frozen=True)
class ParsedSkillMarkdown:
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.body) and not any(d.is_error for d in self.diagnostics)


def parse_skill_markdown(raw: str, *, source: str = "") -> ParsedSkillMarkdown:
    """Parse one ``SKILL.md`` file's text into body + validated metadata.

    A file with no front matter is valid: the whole trimmed text is the body
    and metadata is empty. A file that opens a front-matter block but never
    closes it, or whose block does not parse as a YAML mapping, produces an
    error diagnostic and an empty body — callers must treat that as invalid,
    never as a body-less skill with guessed metadata.
    """
    text = raw.strip("﻿")
    stripped = text.strip()
    if not stripped:
        return ParsedSkillMarkdown(body="", diagnostics=(error("empty_file", "SKILL.md is empty", source),))

    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        return ParsedSkillMarkdown(body=stripped, metadata={})

    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        candidate = line.strip()
        if candidate == _DELIMITER:
            end_index = index
            break
    if end_index is None:
        return ParsedSkillMarkdown(
            body="",
            diagnostics=(
                error("unclosed_front_matter", "front matter is missing its closing '---'", source),
            ),
        )

    raw_block = "\n".join(lines[1:end_index])
    diagnostics: list[SkillDiagnostic] = []
    try:
        loaded = yaml.safe_load(raw_block) if raw_block.strip() else {}
    except yaml.YAMLError as exc:
        return ParsedSkillMarkdown(
            body="",
            diagnostics=(error("invalid_yaml", f"front matter is not valid YAML: {exc}", source),),
        )

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return ParsedSkillMarkdown(
            body="",
            diagnostics=(
                error("front_matter_not_a_mapping", "front matter must be a YAML mapping", source),
            ),
        )

    metadata, meta_diagnostics = _validate_metadata(loaded, source=source)
    diagnostics.extend(meta_diagnostics)

    body = "\n".join(lines[end_index + 1:]).strip()
    if not body:
        diagnostics.append(error("empty_body", "SKILL.md has no body after front matter", source))

    return ParsedSkillMarkdown(body=body, metadata=metadata, diagnostics=tuple(diagnostics))


def _validate_metadata(raw: dict[str, Any], *, source: str) -> tuple[dict[str, Any], list[SkillDiagnostic]]:
    metadata: dict[str, Any] = {}
    diagnostics: list[SkillDiagnostic] = []

    for key in _KNOWN_STRING_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            diagnostics.append(warning("invalid_field_type", f"'{key}' must be a non-empty string", source))
            continue
        metadata[key] = value.strip()

    for key in _KNOWN_LIST_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            diagnostics.append(
                warning("invalid_field_type", f"'{key}' must be a list of strings", source)
            )
            continue
        metadata[key] = [item.strip() for item in value if item.strip()]

    return metadata, diagnostics
