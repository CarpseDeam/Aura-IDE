"""Normalize web-research Drone receipts into a compact ResearchResult."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchResult:
    ok: bool
    answer: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verified_facts: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    confidence: str = "none"
    trace: list[dict[str, Any]] = field(default_factory=list)
    route_used: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str = ""
    run_id: str = ""
    drone_id: str = ""
    status: str = ""
    usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_drone_receipt(cls, receipt: dict[str, Any]) -> "ResearchResult":
        return normalize_research_result(receipt)


def normalize_research_result(receipt: dict[str, Any]) -> ResearchResult:
    """Normalize the existing Drone runner result or persisted receipt dict."""
    raw = receipt if isinstance(receipt, dict) else {}
    artifact = _artifact_from(raw)
    nested_receipt = raw.get("receipt") if isinstance(raw.get("receipt"), dict) else {}

    route_used = _dict_value(artifact.get("route_used"))
    if not route_used:
        route_used = _dict_value(raw.get("route_used"))
    if not route_used and nested_receipt:
        route_used = _dict_value(nested_receipt.get("route_used"))

    error = str(raw.get("error") or artifact.get("error") or "").strip()
    if not error and nested_receipt:
        errors = nested_receipt.get("errors")
        if isinstance(errors, list) and errors:
            error = str(errors[0])

    ok = bool(raw.get("ok"))
    if not ok and nested_receipt:
        ok = nested_receipt.get("status") == "completed"

    return ResearchResult(
        ok=ok,
        answer=str(artifact.get("answer") or "").strip(),
        sources=_dict_list(artifact.get("sources")),
        evidence=_dict_list(artifact.get("evidence")),
        verified_facts=_string_list(artifact.get("verified_facts")),
        gaps=_string_list(artifact.get("gaps")),
        confidence=str(artifact.get("confidence") or "none").strip().lower() or "none",
        trace=_dict_list(artifact.get("trace")),
        route_used=route_used,
        summary=str(raw.get("summary") or artifact.get("summary") or "").strip(),
        error=error,
        run_id=str(raw.get("run_id") or nested_receipt.get("run_id") or "").strip(),
        drone_id=str(raw.get("drone_id") or nested_receipt.get("drone_id") or "").strip(),
        status=str(raw.get("status") or nested_receipt.get("status") or "").strip(),
    )


def format_research_answer(result: ResearchResult | dict[str, Any]) -> str:
    """Build compact sourced chat prose from a normalized research result."""
    normalized = result if isinstance(result, ResearchResult) else normalize_research_result(result)
    if not normalized.ok:
        return f"Web research failed: {normalized.error or normalized.summary or 'Unknown error'}"

    if normalized.confidence in {"none", ""} or (
        not normalized.sources and not normalized.evidence
    ):
        lines = ["I could not verify an answer from live evidence."]
        if normalized.gaps:
            lines.append("Gaps: " + "; ".join(normalized.gaps[:3]))
        return "\n".join(lines)

    lines: list[str] = []
    if normalized.answer:
        lines.append(normalized.answer)
    elif normalized.verified_facts:
        lines.append("Verified facts:")
        lines.extend(f"- {fact}" for fact in normalized.verified_facts[:5])
    else:
        lines.append("Web research found evidence, but did not extract a concise answer.")

    source_lines = _format_sources(normalized.sources)
    if source_lines:
        lines.append("")
        lines.append("Sources:")
        lines.extend(source_lines)

    if normalized.gaps or normalized.confidence == "low":
        lines.append("")
        if normalized.gaps:
            lines.append("Not fully verified: " + "; ".join(normalized.gaps[:3]))
        else:
            lines.append("Confidence is low.")
    return "\n".join(lines)


def _artifact_from(raw: dict[str, Any]) -> dict[str, Any]:
    cargo = raw.get("cargo")
    if isinstance(cargo, dict) and cargo:
        return cargo

    nested_receipt = raw.get("receipt")
    if isinstance(nested_receipt, dict):
        produced = nested_receipt.get("produced_artifact")
        if isinstance(produced, dict) and produced:
            return produced

    produced = raw.get("produced_artifact")
    if isinstance(produced, dict) and produced:
        return produced

    if any(key in raw for key in ("answer", "sources", "evidence", "verified_facts")):
        return raw
    return {}


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(_jsonable_dict(item))
        elif str(item).strip():
            normalized.append({"text": str(item).strip()})
    return normalized


def _jsonable_dict(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        text_key = str(key)
        if isinstance(item, dict):
            result[text_key] = _jsonable_dict(item)
        elif isinstance(item, list):
            result[text_key] = [
                _jsonable_dict(child) if isinstance(child, dict) else child
                for child in item
                if _is_json_scalar(child) or isinstance(child, (dict, list))
            ]
        elif _is_json_scalar(item):
            result[text_key] = item
        else:
            result[text_key] = str(item)
    return result


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _format_sources(sources: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for source in sources[:5]:
        title = str(source.get("title") or source.get("name") or "Source").strip()
        url = str(source.get("url") or "").strip()
        if url:
            lines.append(f"- {title}: {url}")
        elif title:
            lines.append(f"- {title}")
    return lines
