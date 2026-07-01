"""Shell pipeline analysis utilities for worker completion processing."""

from __future__ import annotations

import re
import shlex
from typing import Any


def _is_benign_search_no_match(result: dict[str, Any]) -> bool:
    command = str(result.get("command") or "").strip()
    exit_code = result.get("exit_code")
    output = str(result.get("output") or result.get("output_preview") or "").strip()

    if exit_code != 1 or not command:
        return False
    if output and not _is_no_match_only_output(output):
        return False

    segments = _split_simple_pipeline(command)
    if not segments:
        return False
    return all(_pipeline_segment_starts_with_search(segment) for segment in segments)


def _is_no_match_only_output(output: str) -> bool:
    normalized = re.sub(r"\s+", " ", output.strip().lower())
    return normalized in {
        "no match",
        "no matches",
        "no matches found",
    }


def _split_simple_pipeline(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False

    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            continue
        if char == "|":
            segment = "".join(current).strip()
            if not segment:
                return []
            segments.append(segment)
            current = []
            continue
        current.append(char)

    if quote:
        return []
    segment = "".join(current).strip()
    if not segment:
        return []
    segments.append(segment)
    return segments


def _pipeline_segment_starts_with_search(segment: str) -> bool:
    if re.search(r"(^|[^|])(?:&&|\|\||;|[<>])", segment):
        return False
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = tokens[0].strip("'\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable in {"rg", "grep", "findstr"}
