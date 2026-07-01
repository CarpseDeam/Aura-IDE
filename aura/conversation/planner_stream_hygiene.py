"""Visible planner stream hygiene.

Planner pre-dispatch prose is often streamed before the manager knows whether
the turn will end in a tool call. This helper removes generic transition and
fake-implementation narration from visible planner deltas while preserving real
answers, questions, blockers, tool events, SpecCards, and final receipts.
"""
from __future__ import annotations

import re


_BAD_PHRASE_PATTERNS = (
    r"\blet me implement\b",
    r"\blet me prepare (?:the )?(?:capsule|worker capsule|task capsule)\b",
    r"\b(?:i\s+)?(?:can'?t|cannot) write files directly\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) create\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) update\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) edit\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) modify\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) refactor\b",
    r"\bnow i\s*(?:will|['\u2019]?ll) extract\b",
    r"\b(?:i\s*(?:will|['\u2019]?ll|ill) (?:create|update|edit|modify|implement|refactor|extract|move|rename|remove|delete))\b",
    r"\bi have (?:all|enough|the) context\b",
    r"\bnow i have (?:a )?(?:thorough |complete |full )?(?:understanding|picture|context)\b",
)

_GENERIC_TRANSITION_PATTERNS = (
    r"^\s*now[, ]+(?:i|let me)\b.*\b(?:context|understanding|picture|implement|create|update|edit|modify|refactor|extract|capsule|dispatch)\b",
    r"^\s*(?:now\s+)?let me\b.*\b(?:prepare|implement|create|update|edit|modify|refactor|extract|dispatch|capsule)\b",
    r"^\s*i (?:have|now have)\b.*\b(?:context|understanding|picture)\b",
)

_BOUNDARY_RE = re.compile(r"(?<=[.!?])(\s+)|(\n+)")


class PlannerStreamHygiene:
    """Filter planner-visible filler without touching tool/final surfaces."""

    def __init__(self) -> None:
        self._buffer = ""
        self._suppressed_normalized: set[str] = set()

    def filter_delta(self, text: str) -> str:
        self._buffer += str(text or "")
        return "".join(self._drain_complete_units())

    def flush(self) -> str:
        if not self._buffer:
            return ""
        unit = self._buffer
        self._buffer = ""
        return "" if self._should_suppress(unit) else unit

    def sanitize_message_text(self, text: str) -> str:
        """Return message content with the same hygiene applied deterministically."""
        previous_buffer = self._buffer
        self._buffer = ""
        visible = self.filter_delta(str(text or ""))
        visible += self.flush()
        self._buffer = previous_buffer
        return visible.strip()

    def _drain_complete_units(self, *, force_all: bool = False) -> list[str]:
        units: list[str] = []
        while self._buffer:
            if force_all:
                unit = self._buffer
                self._buffer = ""
            else:
                match = _BOUNDARY_RE.search(self._buffer)
                if match is None:
                    break
                end = match.end()
                unit = self._buffer[:end]
                self._buffer = self._buffer[end:]
            if not self._should_suppress(unit):
                units.append(unit)
        return units

    def _should_suppress(self, unit: str) -> bool:
        text = " ".join(str(unit or "").strip().split())
        if not text:
            return False
        normalized = text.lower()
        if self._looks_like_real_question_or_blocker(normalized) and not self._has_bad_phrase(normalized):
            return False
        if self._has_bad_phrase(normalized) or self._has_generic_transition(normalized):
            self._suppressed_normalized.add(normalized)
            return True
        if normalized in self._suppressed_normalized:
            return True
        return False

    @staticmethod
    def _looks_like_real_question_or_blocker(normalized: str) -> bool:
        return (
            "?" in normalized
            or normalized.startswith(("blocked", "i need ", "please provide", "which "))
            or "need you to" in normalized
            or "cannot proceed" in normalized
        )

    @staticmethod
    def _has_bad_phrase(normalized: str) -> bool:
        return any(re.search(pattern, normalized) for pattern in _BAD_PHRASE_PATTERNS)

    @staticmethod
    def _has_generic_transition(normalized: str) -> bool:
        return any(re.search(pattern, normalized) for pattern in _GENERIC_TRANSITION_PATTERNS)


__all__ = ["PlannerStreamHygiene"]
