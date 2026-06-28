"""Reflective-refinement loop for the skills system.

Derives candidates from outcome-join failures, calls the planner-tier
LLM to reflect with hazard traces, and persists improved guard text
to .aura/skills/refined/ as new Skill entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills
from aura.skills.utility import _is_success_status

_log = logging.getLogger(__name__)

_OUTCOME_DB_FILENAME = "skill_outcomes.db"
_HAZARD_DB_FILENAME = "hazards.db"
_REFINED_DIR = ".aura" / Path("skills") / "refined"


@dataclass(frozen=True)
class RefinementCandidate:
    """A candidate for reflection: a skill text that was active during failures."""

    skill_text: str
    task_kinds: tuple[str, ...]
    path_globs: tuple[str, ...]
    model: str | None
    provenance_from: str
    tool_call_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _refined_id(skill_text: str) -> str:
    """Deterministic short ID for a refined skill based on its text."""
    return hashlib.sha256(skill_text.encode("utf-8")).hexdigest()[:16]


def _read_hazard_traces(
    workspace_root: Path,
    tool_call_ids: tuple[str, ...],
) -> dict[str, list[dict]]:
    """Read hazard traces for the given tool_call_ids from .aura/hazards.db.

    Returns a dict mapping tool_call_id -> list of hazard row dicts.
    Returns {} on any failure or missing DB.
    """
    if not tool_call_ids:
        return {}
    db_path = workspace_root / ".aura" / _HAZARD_DB_FILENAME
    if not db_path.is_file():
        return {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in tool_call_ids)
        rows = conn.execute(
            f"SELECT tool_call_id, status, failure_class, error_signature, "
            f"raw_errors FROM hazards WHERE tool_call_id IN ({placeholders})",
            tool_call_ids,
        ).fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            tid = row["tool_call_id"]
            result.setdefault(tid, []).append(dict(row))
        return result
    except Exception:
        _log.debug("Failed to read hazard traces", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()


def _build_reflection_prompt(skill_text: str, traces: list[dict]) -> str:
    """Build a GEPA-shaped prompt string for the reflector LLM.

    The prompt describes the task, shows the existing guard text, presents
    failure traces, and asks for an improved version.
    """
    lines: list[str] = []
    lines.append("You are a skilled safety guard writer. Your task is to improve a")
    lines.append("guard rule that failed to prevent certain failures.")
    lines.append("")
    lines.append("## Existing Guard Text")
    lines.append("")
    lines.append(skill_text)
    lines.append("")
    lines.append("## Failure Traces")
    lines.append("")

    if not traces:
        lines.append("(no failure traces available)")
    else:
        for i, trace in enumerate(traces, 1):
            lines.append(f"### Trace {i}")
            fc = trace.get("failure_class", "unknown")
            es = trace.get("error_signature", "")
            lines.append(f"  failure_class: {fc}")
            lines.append(f"  error_signature: {es}")
            raw_errors_raw = trace.get("raw_errors", "[]")
            if isinstance(raw_errors_raw, str):
                try:
                    raw_errors = json.loads(raw_errors_raw)
                except Exception:
                    raw_errors = [raw_errors_raw]
            else:
                raw_errors = raw_errors_raw or []
            for j, re_item in enumerate(raw_errors[:3]):
                truncated = str(re_item)[:500]
                lines.append(f"  raw_error_{j + 1}: {truncated}")
            lines.append("")

    lines.append("## Instructions")
    lines.append("")
    lines.append("Diagnose why the existing guard text did not prevent the failures")
    lines.append("shown above. Write an improved version that addresses the gaps.")
    lines.append("")
    lines.append("Output ONLY the improved guard text, no preamble, no explanation,")
    lines.append("starting with the line: ### Skill Guard")

    return "\n".join(lines)


def _call_reflector(
    skill_text: str,
    traces: list[dict],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> str | None:
    """Call the planner-tier LLM to reflect on a skill and failure traces.

    Returns the rewritten guard text string on success, None on failure.
    """
    from aura.backends.api import APIAgentBackend
    from aura.client.events import ApiError, ContentDelta, Done
    from aura.settings import load_settings

    if provider is None or model is None:
        try:
            settings = load_settings()
            if provider is None:
                provider = settings.planner_provider
            if model is None:
                model = settings.default_planner_model
        except Exception:
            _log.debug("Failed to resolve provider/model from settings", exc_info=True)
            return None

    prompt = _build_reflection_prompt(skill_text, traces)

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "You are a safety guard writer. Output only the improved guard text.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        backend = APIAgentBackend(provider=provider)
        parts: list[str] = []
        for event in backend.stream(
            messages=messages,
            tools=None,
            model=model,
            thinking="off",
            temperature=0.3,
        ):
            if isinstance(event, ContentDelta):
                parts.append(event.text)
            elif isinstance(event, Done):
                break
            elif isinstance(event, ApiError):
                _log.debug("Reflector API error: %s", event)
                return None
        result = "".join(parts).strip()
        return result if result else None
    except Exception:
        _log.debug("Reflector call failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_refinement_candidates(
    workspace_root: Path,
    *,
    min_failures: int = 3,
) -> list[RefinementCandidate]:
    """Derive refinement candidates from outcome-join failures.

    Opens .aura/skill_outcomes.db, finds failed rows, re-derives which
    skills would have been loaded for each failed task, and returns
    candidates for skill texts that appear in >= min_failures distinct
    tool_call_ids.
    """
    db_path = workspace_root / ".aura" / _OUTCOME_DB_FILENAME
    if not db_path.is_file():
        return []

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT tool_call_id, status, task_kind, target_files "
            "FROM outcome_joins"
        ).fetchall()

        # Filter to failures (not-success)
        failed_rows = [r for r in rows if not _is_success_status(r["status"])]
        if not failed_rows:
            return []

        # Read current skill pool once
        skills = read_skills(workspace_root)

        # Accumulate skill_text -> set of tool_call_ids
        text_to_ids: dict[str, set[str]] = {}

        for row in failed_rows:
            tid = row["tool_call_id"]
            task_kind = row.get("task_kind") or None
            raw_targets = row.get("target_files")
            if isinstance(raw_targets, str) and raw_targets:
                try:
                    target_files = tuple(json.loads(raw_targets))
                except Exception:
                    target_files = ()
            elif isinstance(raw_targets, (list, tuple)):
                target_files = tuple(raw_targets)
            else:
                target_files = ()

            # Re-derive which skills would have been loaded
            loaded = select_relevant_skills(
                skills,
                task_kind=task_kind,
                target_files=target_files,
            )

            for skill in loaded:
                text_to_ids.setdefault(skill.text, set()).add(tid)

        # Build candidates
        candidates: list[RefinementCandidate] = []
        for text, ids in text_to_ids.items():
            if len(ids) >= min_failures:
                # Find a matching Skill for the metadata
                for skill in skills:
                    if skill.text == text:
                        candidates.append(
                            RefinementCandidate(
                                skill_text=text,
                                task_kinds=skill.task_kinds,
                                path_globs=skill.path_globs,
                                model=skill.model,
                                provenance_from=skill.provenance.value,
                                tool_call_ids=tuple(sorted(ids)),
                            )
                        )
                        break

        return candidates
    except Exception:
        _log.debug("derive_refinement_candidates failed", exc_info=True)
        return []
    finally:
        if conn is not None:
            conn.close()


def reflect_on_candidate(
    workspace_root: Path,
    candidate: RefinementCandidate,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> str | None:
    """Reflect on a single candidate using the planner LLM and hazard traces.

    Returns the rewritten guard text string or None on failure.
    """
    traces = _read_hazard_traces(workspace_root, candidate.tool_call_ids)
    if not traces:
        return None

    # Flatten all traces for the candidate's tool_call_ids
    all_records: list[dict] = []
    for tid in candidate.tool_call_ids:
        all_records.extend(traces.get(tid, []))
    if not all_records:
        return None

    return _call_reflector(
        candidate.skill_text,
        all_records,
        provider=provider,
        model=model,
    )


def persist_refinement(
    workspace_root: Path,
    candidate: RefinementCandidate,
    rewritten_text: str,
) -> Path | None:
    """Write a refined skill JSON file to .aura/skills/refined/.

    Returns the written Path on success, None on failure.
    Never writes to bundled/ or hazards.db.
    """
    try:
        refined_dir = workspace_root / _REFINED_DIR
        refined_dir.mkdir(parents=True, exist_ok=True)

        skill_id = _refined_id(candidate.skill_text)
        dest = refined_dir / f"{skill_id}.json"

        data: dict[str, Any] = {
            "text": rewritten_text,
            "task_kinds": list(candidate.task_kinds),
            "path_globs": list(candidate.path_globs),
            "model": candidate.model,
            "provenance": "reflection_refined",
            "origin": [
                ["provenance_from", candidate.provenance_from],
                ["original_text_prefix", candidate.skill_text[:120]],
                [
                    "triggering_tool_call_ids",
                    json.dumps(list(candidate.tool_call_ids)),
                ],
            ],
        }

        dest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return dest
    except Exception:
        _log.debug("persist_refinement failed", exc_info=True)
        return None
