"""Safe, durable metadata for provider-hosted web search."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

AURA_HOSTED_SEARCH_KEY = "aura_hosted_search"


def attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def safe_web_url(raw: Any) -> str:
    """Return a display-safe HTTP(S) URL, or ``""`` for unsafe schemes."""
    url = str(raw or "").strip()
    if not url:
        return ""
    marker = url.find("#ws_call_id=")
    if marker != -1:
        url = url[:marker].rstrip("#")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    if any(ord(char) < 32 or char in {'"', "'", "<", ">", "`"} for char in url):
        return ""
    return url


def citation_from(value: Any) -> dict[str, str] | None:
    """Normalize one provider citation/search-result object."""
    nested = attr(value, "url_citation")
    source = nested if nested is not None else value
    url = safe_web_url(attr(source, "url", "") or attr(source, "uri", ""))
    if not url:
        web = attr(source, "web")
        url = safe_web_url(attr(web, "uri", "") or attr(web, "url", ""))
        source = web if web is not None else source
    if not url:
        return None
    title = str(attr(source, "title", "") or "").strip()
    return {"title": title, "url": url}


def citations_from_response_item(item: Any) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    content = attr(item, "content")
    if not isinstance(content, list):
        return citations
    for part in content:
        annotations = attr(part, "annotations")
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if attr(annotation, "type", "") != "url_citation":
                continue
            citation = citation_from(annotation)
            if citation is not None:
                citations.append(citation)
    return citations


def dedupe_citations(values: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for value in values:
        citation = citation_from(value)
        if citation is None or citation["url"] in seen:
            continue
        seen.add(citation["url"])
        result.append(citation)
    return result


def citation_markdown(citations: list[dict[str, str]], existing_text: str) -> str:
    """Build a compact clickable source suffix through Aura's Markdown path."""
    safe = dedupe_citations(citations)
    missing = [item for item in safe if item["url"] not in existing_text]
    if not missing:
        return ""
    links = []
    for index, item in enumerate(missing, start=1):
        label = _markdown_label(item.get("title") or f"Source {index}")
        links.append(f"[{label}](<{item['url']}>)")
    prefix = "\n\nSources: " if existing_text else "Sources: "
    return prefix + " · ".join(links)


def hosted_search_metadata(
    *,
    provider: str,
    tool_type: str,
    citations: list[dict[str, str]],
    calls: list[dict[str, Any]] | None = None,
    wire_blocks: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return JSON-safe Aura metadata only when hosted search occurred."""
    normalized_citations = dedupe_citations(citations)
    normalized_calls = [copy.deepcopy(call) for call in (calls or [])]
    normalized_blocks = [copy.deepcopy(block) for block in (wire_blocks or [])]
    normalized_usage = copy.deepcopy(usage) if isinstance(usage, dict) else None
    if (
        not normalized_citations
        and not normalized_calls
        and not normalized_blocks
        and not normalized_usage
    ):
        return None
    metadata: dict[str, Any] = {
        "provider": provider,
        "tool_type": tool_type,
        "citations": normalized_citations,
    }
    if normalized_calls:
        metadata["calls"] = normalized_calls
        metadata["search_count"] = len(normalized_calls)
    if normalized_blocks:
        metadata["wire_blocks"] = normalized_blocks
    if normalized_usage:
        metadata["usage"] = normalized_usage
    return metadata


def matching_wire_blocks(message: Mapping[str, Any], provider: str) -> list[dict[str, Any]]:
    metadata = message.get(AURA_HOSTED_SEARCH_KEY)
    if not isinstance(metadata, Mapping) or metadata.get("provider") != provider:
        return []
    blocks = metadata.get("wire_blocks")
    if not isinstance(blocks, list):
        return []
    return [copy.deepcopy(block) for block in blocks if isinstance(block, dict)]


def _markdown_label(value: str) -> str:
    label = " ".join(str(value).split()) or "Source"
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


__all__ = [
    "AURA_HOSTED_SEARCH_KEY",
    "citation_from",
    "citation_markdown",
    "citations_from_response_item",
    "dedupe_citations",
    "hosted_search_metadata",
    "matching_wire_blocks",
    "safe_web_url",
]
