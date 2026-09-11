"""OpenRouter reasoning stream projection and lossless continuation payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class OpenRouterReasoning:
    def __init__(self) -> None:
        self.details: list[dict[str, Any]] = []
        self._text = {"reasoning": "", "reasoning_content": "", "details": ""}
        self.display = ""
        self._source: str | None = None

    def push(self, delta: Any) -> str:
        for key in ("reasoning", "reasoning_content"):
            value = getattr(delta, key, None)
            if isinstance(value, str):
                self._text[key] += value
        for detail in getattr(delta, "reasoning_details", None) or []:
            if hasattr(detail, "model_dump"):
                detail = detail.model_dump(exclude_unset=True)
            if not isinstance(detail, dict):
                continue
            # Keep the exact sequence, including signatures, encrypted data,
            # indexes, unknown blocks and fields. Do not merge/reorder by index.
            self.details.append(deepcopy(detail))
            key = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(detail.get("type"))
            value = detail.get(key) if key else None
            if isinstance(value, str):
                self._text["details"] += value

        if self._source is None:
            self._source = next((k for k, v in self._text.items() if v), None)
        if self._source is None:
            return ""
        # These are alternative representations of reasoning. Compare whole
        # accumulated prefixes so mirrored fields can arrive in different
        # chunks without duplicating displayed text. Never trim replay data.
        candidate = self._text[self._source]
        for value in self._text.values():
            if value.startswith(candidate) and len(value) > len(candidate):
                candidate = value
        if not candidate.startswith(self.display):
            return ""
        suffix = candidate[len(self.display):]
        self.display = candidate
        return suffix

    def message_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        reasoning = self._text["reasoning"] or self._text["reasoning_content"]
        if reasoning:
            fields["reasoning"] = reasoning
        if self.display:
            # Aura's existing transcript readers use this display projection.
            fields["reasoning_content"] = self.display
        if self.details:
            fields["reasoning_details"] = deepcopy(self.details)
        return fields
