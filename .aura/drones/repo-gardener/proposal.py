"""Request a single bounded refactor proposal via one model call."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from picker import Chosen


@dataclass(frozen=True)
class EditProposal:
    files: tuple[str, ...]
    new_contents: dict[str, str]
    rationale: str
    expected_diff_lines: int


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

def _extract_excerpt(source: str, center_line: int, radius: int = 25) -> str:
    """Extract an excerpt around *center_line* (1-based)."""
    lines = source.splitlines()
    start = max(0, center_line - 1 - radius)
    end = min(len(lines), center_line - 1 + radius + 1)
    excerpt = lines[start:end]
    result: list[str] = []
    for i, line in enumerate(excerpt):
        real_ln = start + i + 1
        result.append(f"{real_ln:>6}: {line}")
    return "\n".join(result)


def build_edit_request(
    chosen: Chosen,
    source: str,
    imports: list[str],
    call_sites: list[str],
) -> str:
    """Build a tight prompt for the model.

    *chosen* — the target from the picker.
    *source* — complete file source.
    *imports* — relevant import lines.
    *call_sites* — relevant call-site snippets.
    """
    excerpt = _extract_excerpt(source, chosen.line, radius=30)

    lines = [
        "You are editing a file in the aura/ project.",
        "",
        f"FILE: {chosen.path}",
        "",
        "PROBLEM:",
        f"  {chosen.reason}",
        "",
        "TARGET EXCERPT (with line numbers):",
        excerpt,
        "",
        "IMPORTS IN THIS FILE:",
    ]
    if imports:
        for imp in imports:
            lines.append(f"  {imp}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("CALL SITES THAT REFERENCE THIS CODE:")
    if call_sites:
        for cs in call_sites:
            lines.append(f"  {cs}")
    else:
        lines.append("  (none)")

    lines.extend([
        "",
        "CONSTRAINTS:",
        "  - Edit at most 2 files (prefer 1).",
        "  - Change at most ~60 lines total.",
        "  - Do NOT change public API (__all__ exports, top-level public names).",
        "  - Do NOT rename existing symbols.",
        "  - Do NOT delete files.",
        "  - Do NOT add new imports unless essential for the edit.",
        "  - Keep the edit as small as possible.",
        "  - Validation: py_compile must pass.",
        "",
        "RESPOND ONLY with a valid JSON object in exactly this format:",
        """{"files": [{"path": "<workspace-relative path>", "content": "<full new file content>"}], "rationale": "<short explanation>", "expected_diff_lines": <int>}""",
        "",
        "Each 'content' field MUST be the COMPLETE new file content (not a patch).",
        "If you make no changes, return {\"files\": [], \"rationale\": \"no edit needed\", \"expected_diff_lines\": 0}.",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

def _openai_chat(packet: str, api_key: str, model: str, base_url: str) -> str | None:
    """Call an OpenAI-compatible chat completions API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": packet}],
        "temperature": 0.1,
        "max_tokens": 4096,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        return content
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError) as exc:
        import sys
        print(f"[proposal] API call failed: {exc}", file=sys.stderr)
        return None


def _resolve_config() -> tuple[dict[str, str] | None, str]:
    """Read provider config from environment variables.

    Returns (config_dict, rationale) where config_dict has keys:
      api_key, model, base_url, provider_id

    If config is unavailable, returns (None, rationale_string).
    """
    aura_provider_id = os.environ.get("AURA_PROVIDER_ID", "")
    aura_model = os.environ.get("AURA_MODEL", "")
    aura_base_url = os.environ.get("AURA_BASE_URL", "")
    aura_api_key = os.environ.get("AURA_API_KEY", "")

    aura_vars_set = any(v for v in [aura_provider_id, aura_model, aura_base_url, aura_api_key])

    if aura_vars_set:
        # Aura runtime — all AURA_* vars must be present
        missing = []
        if not aura_model:
            missing.append("AURA_MODEL")
        if not aura_base_url:
            missing.append("AURA_BASE_URL")
        if not aura_api_key:
            missing.append("AURA_API_KEY")
        if not aura_provider_id:
            missing.append("AURA_PROVIDER_ID")

        if missing:
            return None, f"Aura provider config unavailable: missing {', '.join(missing)}"

        return {
            "api_key": aura_api_key,
            "model": aura_model,
            "base_url": aura_base_url,
            "provider_id": aura_provider_id,
        }, ""

    # Pure local dev — fall back to OPENAI_* vars
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base_url = os.environ.get("OPENAI_BASE_URL", "")
    openai_model = os.environ.get("OPENAI_MODEL", "")

    missing = []
    if not openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not openai_base_url:
        missing.append("OPENAI_BASE_URL")
    if not openai_model:
        missing.append("OPENAI_MODEL")

    if missing:
        return None, "Aura provider config unavailable"

    return {
        "api_key": openai_api_key,
        "model": openai_model,
        "base_url": openai_base_url,
        "provider_id": "openai",
    }, ""


def request_edit_proposal(packet: str) -> EditProposal:
    """Make ONE model call to produce an EditProposal.

    Reads provider config via ``_resolve_config()`` from environment variables.
    Returns an empty ``EditProposal`` if the API is unavailable or parsing fails.
    """
    import sys

    config, rationale = _resolve_config()
    if config is None:
        print(f"[proposal] {rationale}", file=sys.stderr)
        return EditProposal(
            files=(),
            new_contents={},
            rationale=rationale,
            expected_diff_lines=0,
        )

    api_key = config["api_key"]
    model = config["model"]
    base_url = config["base_url"]
    provider_id = config["provider_id"]

    print(f"[proposal] provider_id={provider_id} model={model} base_url={base_url}", file=sys.stderr)
    content = _openai_chat(packet, api_key, model, base_url)

    if not content:
        return EditProposal(
            files=(),
            new_contents={},
            rationale="API returned no content",
            expected_diff_lines=0,
        )

    # Parse JSON from the response
    try:
        # Strip markdown fences if present
        cleaned = content.strip()
        if cleaned.startswith("```"):
            # Find the first { and last }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start:end+1]
        elif cleaned.startswith("```json"):
            cleaned = cleaned.removeprefix("```json").strip()
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start:end+1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[proposal] JSON parse failed: {exc}", file=sys.stderr)
        print(f"[proposal] Raw content (first 500 chars): {content[:500]}", file=sys.stderr)
        return EditProposal(
            files=(),
            new_contents={},
            rationale=f"Failed to parse model response: {exc}",
            expected_diff_lines=0,
        )

    files_list = data.get("files", [])
    new_contents: dict[str, str] = {}
    file_paths: list[str] = []
    for entry in files_list:
        fp = entry.get("path", "unknown")
        content = entry.get("content", "")
        new_contents[fp] = content
        file_paths.append(fp)

    rationale = data.get("rationale", "No rationale provided")
    expected_diff_lines = data.get("expected_diff_lines", 0)

    return EditProposal(
        files=tuple(file_paths),
        new_contents=new_contents,
        rationale=rationale,
        expected_diff_lines=expected_diff_lines,
    )


# ---------------------------------------------------------------------------
# Extract-method support (model identifies, deterministic moves)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractProposal:
    start_line: int
    end_line: int
    helper_name: str
    rationale: str


def build_extract_request(
    chosen: Chosen,
    source: str,
) -> str:
    """Build a prompt asking the model to identify an extractable block.

    The model returns only metadata (lines, name, rationale), NOT code.
    """
    lines = [
        "You are helping refactor a Python file by extracting a block of statements",
        "into a same-file helper function. You do NOT write any code.",
        "",
        f"FILE: {chosen.path}",
        "",
        "PROBLEM:",
        f"  {chosen.reason}",
        "",
        "FULL FILE SOURCE (with line numbers):",
    ]

    src_lines = source.splitlines()
    for i, line in enumerate(src_lines):
        lines.append(f"{i+1:>6}: {line}")

    lines.extend([
        "",
        "TASK:",
        f"The function `{chosen.detail}` is oversized. Identify a contiguous",
        "block of top-level statements inside it that can be extracted into a helper.",
        "",
        "RULES:",
        "- The block must be a contiguous run of statements at the function's top body level (not inside nested if/for/try).",
        "- The block must NOT contain: return, yield, break, continue, global, nonlocal, await, nested def/class, del, walrus (:=), or except ... as name.",
        "",
        "Respond with JSON only:",
        '{',
        '  "start_line": <int, 1-based line of first statement in block>,'
        '  "end_line": <int, 1-based line of last statement in block>,'
        '  "helper_name": "<str, a valid Python identifier for the new helper>",'
        '  "rationale": "<str, one sentence why this block is extractable>"'
        '}',
        "",
        "Do NOT include any code. Do NOT explain beyond the rationale field.",
    ])

    return "\n".join(lines)


def request_extract_proposal(packet: str) -> ExtractProposal:
    """Make ONE model call to produce an ExtractProposal.

    Falls back to empty ExtractProposal on any failure.
    Reuses the same provider config and API call as ``request_edit_proposal``.
    """
    import sys

    config, rationale = _resolve_config()
    if config is None:
        print(f"[proposal] {rationale}", file=sys.stderr)
        return ExtractProposal(
            start_line=0, end_line=0, helper_name="", rationale=rationale
        )

    api_key = config["api_key"]
    model = config["model"]
    base_url = config["base_url"]
    provider_id = config["provider_id"]

    print(
        f"[proposal] extract: provider_id={provider_id} model={model}",
        file=sys.stderr,
    )
    content = _openai_chat(packet, api_key, model, base_url)

    if not content:
        return ExtractProposal(
            start_line=0,
            end_line=0,
            helper_name="",
            rationale="API returned no content",
        )

    # Parse JSON (strip markdown fences)
    cleaned = content.strip()
    if cleaned.startswith("```"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return ExtractProposal(
            start_line=0,
            end_line=0,
            helper_name="",
            rationale="model returned invalid JSON",
        )

    start_line = data.get("start_line", 0)
    end_line = data.get("end_line", 0)
    helper_name = data.get("helper_name", "")
    rationale = data.get("rationale", "")

    if (
        not isinstance(start_line, int)
        or not isinstance(end_line, int)
        or not helper_name
    ):
        return ExtractProposal(
            start_line=0,
            end_line=0,
            helper_name="",
            rationale="incomplete ExtractProposal fields",
        )

    return ExtractProposal(
        start_line=start_line,
        end_line=end_line,
        helper_name=helper_name,
        rationale=rationale,
    )
