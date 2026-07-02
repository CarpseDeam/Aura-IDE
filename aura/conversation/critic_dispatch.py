from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aura.client import Done
from aura.conversation.critic_verdict import CriticFinding, CriticVerdict
from aura.conversation.dispatch import WorkerDispatchRequest
from aura.model_streams import model_streams
from aura.paths import safe_is_relative_to, safe_relative_to
from aura.roles import load_bundled_named_role_capsule

_log = logging.getLogger(__name__)

CriticCallback = Callable[[str, "CriticRequest"], CriticVerdict]

_EDIT_TOOL_NAMES = {
    "write_file",
    "patch_file",
    "delete_file",
    "run_terminal_command",
    "run_and_watch",
    "summon_drone",
}

_FALLBACK_CRITIC_SYSTEM_PROMPT = """You are Aura's invisible dispatch critic.

Judge whether the worker's final implementation conforms to the planner's WorkerDispatchRequest.
The acceptance field is the definition-of-done.
The authoritative contract is: goal, spec, acceptance, required_outputs, expected_public_symbols, expected_dataclass_fields, forbidden_calls, forbidden_public_methods, allowed_responsibilities, forbidden_responsibilities, and non_goals.
Judge intent-vs-implementation only.
Do not review style, taste, architecture preference, or broad quality unless directly tied to a cited contract clause.
Return only one strict JSON object with this shape:
{"conforms": true|false, "route": "release"|"worker"|"planner", "findings": [{"clause": "...", "file": "...", "message": "...", "suggested_action": "..."}], "instruction": "...", "planner_question": "..."}

Rules:
- Every finding must cite a concrete clause in "clause"; findings with no clause are inadmissible.
- Use route "worker" only when the request was achievable and the worker missed it.
- Use route "planner" only when the request is contradictory, impossible, underspecified in a way that blocks release, or requires a product decision.
- Use route "release" when the diff conforms or the request lacks a concrete clause to judge.
- Never propose broad redesign.
- Never expand scope.
- Never mention the critic.
- Return strict JSON only.
"""


def _critic_system_prompt() -> str:
    capsule = load_bundled_named_role_capsule("critic", allowed={"critic"})
    if capsule is not None:
        return capsule.content
    return _FALLBACK_CRITIC_SYSTEM_PROMPT


CRITIC_SYSTEM_PROMPT = _critic_system_prompt()


@dataclass
class CriticRequest:
    original_request: WorkerDispatchRequest
    diff_text: str
    workspace_root: str | Path | None = None
    changed_files: list[str] = field(default_factory=list)
    final_file_texts: dict[str, str] = field(default_factory=dict)
    deterministic_findings: list[CriticFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.diff_text = str(self.diff_text or "")
        self.changed_files = _normalize_path_strings(self.changed_files)
        self.final_file_texts = _coerce_final_file_texts(self.final_file_texts)
        if self.workspace_root is not None and not self.final_file_texts:
            self.final_file_texts = _read_relevant_final_files(
                Path(self.workspace_root),
                self.original_request,
                self.changed_files,
            )
        if not self.deterministic_findings:
            self.deterministic_findings = deterministic_critic_findings(
                self.original_request,
                self.diff_text,
                final_file_texts=self.final_file_texts,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_request": _request_contract(self.original_request),
            "diff_text": self.diff_text,
            "changed_files": list(self.changed_files),
            "checked_final_files": sorted(self.final_file_texts),
            "deterministic_findings": [
                finding.to_dict() for finding in self.deterministic_findings
            ],
        }


def evaluate_deterministic_critic_request(request: CriticRequest) -> CriticVerdict | None:
    if not request.deterministic_findings:
        return None
    return CriticVerdict(
        conforms=False,
        route="worker",
        findings=list(request.deterministic_findings),
        instruction=_worker_instruction_from_findings(request.deterministic_findings),
    )


def deterministic_critic_findings(
    request: WorkerDispatchRequest,
    diff_text: str,
    *,
    final_file_texts: dict[str, str] | None = None,
) -> list[CriticFinding]:
    added_text = _added_diff_text(diff_text)
    final_texts = _coerce_final_file_texts(final_file_texts or {})
    findings: list[CriticFinding] = []

    for symbol in request.expected_public_symbols:
        expected = str(symbol or "").strip()
        if not expected:
            continue
        symbol_file = _final_file_with_identifier(final_texts, expected)
        if symbol_file:
            continue
        if not final_texts and _contains_identifier(added_text, expected):
            continue
        findings.append(
            CriticFinding(
                clause=f"expected_public_symbols: {expected}",
                file=_preferred_finding_file(request, final_texts),
                message=f"Expected public symbol '{expected}' is not present in the final relevant files.",
                suggested_action=(
                    f"Add or expose the requested public symbol '{expected}', or report why it cannot be done."
                ),
            )
        )

    for class_name, fields in request.expected_dataclass_fields.items():
        dataclass_name = str(class_name or "").strip()
        if not dataclass_name or not isinstance(fields, list):
            continue
        for raw_field in fields:
            field_name = str(raw_field or "").strip()
            if not field_name:
                continue
            field_file = _final_file_with_dataclass_field(
                final_texts,
                dataclass_name,
                field_name,
            )
            if field_file:
                continue
            if not final_texts:
                continue
            findings.append(
                CriticFinding(
                    clause=f"expected_dataclass_fields: {dataclass_name}.{field_name}",
                    file=_preferred_finding_file(request, final_texts),
                    message=(
                        f"Expected dataclass field '{field_name}' on '{dataclass_name}' "
                        "is not present in the final relevant files."
                    ),
                    suggested_action=(
                        f"Add dataclass field '{field_name}' to '{dataclass_name}', "
                        "or report why the requested field cannot be provided."
                    ),
                )
            )

    for forbidden in [*request.forbidden_calls, *request.forbidden_public_methods]:
        item = str(forbidden or "").strip()
        if not item:
            continue
        if not _contains_forbidden_reference(added_text, item):
            continue
        findings.append(
            CriticFinding(
                clause=f"forbidden_calls/forbidden_public_methods: {item}",
                file=_first_request_file(request),
                message=f"Worker diff introduces forbidden reference '{item}'.",
                suggested_action=f"Remove '{item}' and use an allowed implementation path.",
            )
        )

    return findings


def run_critic_dispatch(
    tool_call_id: str,
    request: CriticRequest,
    *,
    model: Any,
    thinking: Any,
    temperature: float = 0.0,
    hook_name: str = "generate_worker_code",
    cancel_event: threading.Event | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> CriticVerdict:
    deterministic = evaluate_deterministic_critic_request(request)
    if deterministic is not None:
        return deterministic

    safe_tools = _without_edit_tools(tools or [])
    messages = _critic_messages(request)
    final_message: dict[str, Any] | None = None
    try:
        for ev in model_streams.trigger(
            hook_name,
            messages=messages,
            tools=safe_tools,
            model=model,
            thinking=thinking,
            cancel_event=cancel_event or threading.Event(),
            temperature=temperature,
        ):
            if isinstance(ev, Done):
                final_message = ev.full_message
    except Exception:
        _log.debug("critic_dispatch_failed_open tool_call_id=%s", tool_call_id, exc_info=True)
        return CriticVerdict.release()

    content = ""
    if isinstance(final_message, dict):
        content = str(final_message.get("content") or "")
    return parse_critic_verdict(content)


def parse_critic_verdict(content: str) -> CriticVerdict:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = _strip_markdown_fence(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        _log.debug("critic_verdict_malformed_json")
        return CriticVerdict.release()
    return CriticVerdict.from_dict(parsed)


def _critic_messages(request: CriticRequest) -> list[dict[str, str]]:
    payload = {
        "worker_dispatch_request": _request_contract(request.original_request),
        "worker_unified_diff": request.diff_text,
        "deterministic_precheck": {
            "checked_final_files": sorted(request.final_file_texts),
            "findings": [finding.to_dict() for finding in request.deterministic_findings],
        },
    }
    return [
        {"role": "system", "content": _critic_system_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def _worker_instruction_from_findings(findings: list[CriticFinding]) -> str:
    lines = [
        "Patch only the critic conformance findings.",
        "Do not redesign.",
        "Preserve behavior outside the cited clauses.",
        "Rerun the smallest relevant validation.",
        "",
        "Findings:",
    ]
    for finding in findings:
        location = finding.file or "<workspace>"
        lines.append(
            f"- {location} - {finding.clause}: {finding.message} - {finding.suggested_action}"
        )
    return "\n".join(lines)


def _added_diff_text(diff_text: str) -> str:
    lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        lines.append(line[1:])
    return "\n".join(lines)


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    escaped = re.escape(identifier)
    return bool(re.search(rf"(?<![\w.]){escaped}(?![\w.])", text))


def _final_file_with_identifier(final_file_texts: dict[str, str], identifier: str) -> str:
    for path, text in sorted(final_file_texts.items()):
        if _contains_identifier(text, identifier):
            return path
    return ""


def _final_file_with_dataclass_field(
    final_file_texts: dict[str, str],
    class_name: str,
    field_name: str,
) -> str:
    for path, text in sorted(final_file_texts.items()):
        if _dataclass_field_present(text, class_name, field_name):
            return path
    return ""


def _dataclass_field_present(source: str, class_name: str, field_name: str) -> bool:
    class_match = re.search(
        rf"(?ms)^@(?:dataclasses\.)?dataclass(?:\([^)]*\))?\s*\n"
        rf"(?:@[^\n]+\n)*class\s+{re.escape(class_name)}\b.*?:\n(?P<body>.*?)(?=^\S|\Z)",
        source,
    )
    if not class_match:
        return False
    body = class_match.group("body")
    return bool(re.search(rf"(?m)^\s+{re.escape(field_name)}\s*(?::|=)", body))


def _contains_forbidden_reference(text: str, reference: str) -> bool:
    if not reference:
        return False
    if "(" in reference or "." in reference:
        return reference in text
    escaped = re.escape(reference)
    return bool(re.search(rf"(?<![\w.]){escaped}\s*\(", text))


def _first_request_file(request: WorkerDispatchRequest) -> str:
    return str(request.files[0]) if request.files else ""


def _preferred_finding_file(
    request: WorkerDispatchRequest,
    final_file_texts: dict[str, str],
) -> str:
    first = _first_request_file(request)
    if first:
        return first
    return next(iter(sorted(final_file_texts)), "")


def _request_contract(request: WorkerDispatchRequest) -> dict[str, Any]:
    return {
        "goal": request.goal,
        "files": list(request.files),
        "spec": request.spec,
        "acceptance": request.acceptance,
        "required_outputs": list(request.required_outputs),
        "expected_public_symbols": list(request.expected_public_symbols),
        "expected_dataclass_fields": dict(request.expected_dataclass_fields),
        "forbidden_calls": list(request.forbidden_calls),
        "forbidden_public_methods": list(request.forbidden_public_methods),
        "allowed_responsibilities": list(request.allowed_responsibilities),
        "forbidden_responsibilities": list(request.forbidden_responsibilities),
        "non_goals": list(request.non_goals),
    }


def _normalize_path_strings(paths: list[str]) -> list[str]:
    return sorted({
        str(path).replace("\\", "/").lstrip("/")
        for path in paths
        if str(path or "").strip()
    })


def _coerce_final_file_texts(raw: dict[str, str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(path).replace("\\", "/").lstrip("/"): str(text)
        for path, text in raw.items()
        if str(path or "").strip()
    }


def _read_relevant_final_files(
    workspace_root: Path,
    request: WorkerDispatchRequest,
    changed_files: list[str],
) -> dict[str, str]:
    root = workspace_root.resolve()
    result: dict[str, str] = {}
    for rel_path in _relevant_paths(request, changed_files, root):
        candidate = (root / rel_path).resolve()
        if not safe_is_relative_to(candidate, root):
            continue
        if not candidate.is_file():
            continue
        try:
            result[rel_path] = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return result


def _relevant_paths(
    request: WorkerDispatchRequest,
    changed_files: list[str],
    root: Path,
) -> list[str]:
    paths: list[str] = []
    for raw in [*request.files, *changed_files]:
        rel = _safe_relative_workspace_path(root, raw)
        if rel:
            paths.append(rel)
    return sorted(set(paths))


def _safe_relative_workspace_path(root: Path, raw_path: Any) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / raw.lstrip("/")
    candidate = candidate.resolve()
    if not safe_is_relative_to(candidate, root):
        return ""
    return safe_relative_to(candidate, root).as_posix()


def _without_edit_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_tools: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        if name in _EDIT_TOOL_NAMES:
            continue
        safe_tools.append(tool)
    return safe_tools


def _strip_markdown_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = [
    "CRITIC_SYSTEM_PROMPT",
    "CriticCallback",
    "CriticRequest",
    "deterministic_critic_findings",
    "evaluate_deterministic_critic_request",
    "parse_critic_verdict",
    "run_critic_dispatch",
]
