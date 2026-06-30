"""Deterministic enrichment for Planner -> Worker dispatch contracts."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable, Iterable

from aura.conversation.dispatch import WorkerDispatchRequest

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER_RE = re.compile(rf"^{_IDENT}$")
_DOTTED_CALL_RE = re.compile(rf"^{_IDENT}(?:\.{_IDENT})*$")
_DOTTED_FIELD_RE = re.compile(rf"^(?P<class>{_IDENT})\.(?P<field>{_IDENT})$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_DATACLASS_FIELD_ON_RE = re.compile(
    rf"`(?P<field>{_IDENT})`\s+on\s+`(?P<class>{_IDENT})`",
    re.IGNORECASE,
)


def enrich_worker_dispatch_contract(
    req: WorkerDispatchRequest,
) -> WorkerDispatchRequest:
    """Backfill obvious structured contract fields from explicit prose."""
    texts = _contract_texts(req)
    return replace(
        req,
        expected_public_symbols=_merge_list(
            req.expected_public_symbols,
            _extract_expected_public_symbols(texts),
        ),
        expected_dataclass_fields=_merge_dataclass_fields(
            req.expected_dataclass_fields,
            _extract_expected_dataclass_fields(texts),
        ),
        forbidden_calls=_merge_list(
            req.forbidden_calls,
            _extract_forbidden_calls(texts),
        ),
        forbidden_public_methods=_merge_list(
            req.forbidden_public_methods,
            _extract_forbidden_public_methods(texts),
        ),
    )


def _contract_texts(req: WorkerDispatchRequest) -> tuple[str, ...]:
    values: list[str] = [req.goal, req.spec, req.acceptance]
    values.extend(req.required_outputs)
    return tuple(str(value or "") for value in values if str(value or "").strip())


def _extract_expected_public_symbols(texts: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    for line in _iter_signal_lines(texts, _line_has_public_symbol_signal):
        for item in _backticked_items(line):
            if _is_identifier(item):
                symbols.append(item)
    return _dedupe(symbols)


def _extract_expected_dataclass_fields(
    texts: Iterable[str],
) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in _iter_signal_lines(
        texts,
        _line_has_dataclass_field_signal,
        extra_line_signal=_line_is_dotted_dataclass_field_item,
    ):
        for match in _DATACLASS_FIELD_ON_RE.finditer(line):
            _add_dataclass_field(fields, match.group("class"), match.group("field"))
        for item in _backticked_items(line):
            match = _DOTTED_FIELD_RE.fullmatch(item)
            if match:
                _add_dataclass_field(fields, match.group("class"), match.group("field"))
    return {class_name: _dedupe(class_fields) for class_name, class_fields in fields.items()}


def _extract_forbidden_calls(texts: Iterable[str]) -> list[str]:
    calls: list[str] = []
    for line in _iter_signal_lines(texts, _line_has_forbidden_call_signal):
        for item in _backticked_items(line):
            if _DOTTED_CALL_RE.fullmatch(item):
                calls.append(item)
    return _dedupe(calls)


def _extract_forbidden_public_methods(texts: Iterable[str]) -> list[str]:
    methods: list[str] = []
    for line in _iter_signal_lines(texts, _line_has_forbidden_public_method_signal):
        for item in _backticked_items(line):
            if _is_identifier(item):
                methods.append(item)
    return _dedupe(methods)


def _iter_lines(texts: Iterable[str]) -> Iterable[str]:
    for text in texts:
        yield from str(text or "").splitlines() or [str(text or "")]


def _iter_signal_lines(
    texts: Iterable[str],
    line_signal: Callable[[str], bool],
    *,
    extra_line_signal: Callable[[str], bool] | None = None,
) -> Iterable[str]:
    in_section = False
    for line in _iter_lines(texts):
        stripped = line.strip()
        has_signal = line_signal(line)
        has_extra_signal = bool(extra_line_signal and extra_line_signal(line))
        if has_signal or has_extra_signal:
            yield line
            in_section = has_signal and (stripped.endswith(":") or not _backticked_items(line))
            continue
        if in_section and stripped.startswith(("-", "*")):
            yield line
            continue
        if stripped:
            in_section = False
        else:
            in_section = False


def _backticked_items(text: str) -> list[str]:
    return [match.group(1).strip() for match in _BACKTICK_RE.finditer(text)]


def _line_has_public_symbol_signal(line: str) -> bool:
    text = line.lower()
    return bool(
        re.search(r"\bexpected\s+public\s+symbols?\b", text)
        or re.search(r"\b(?:expose|export)\b\s+`", text)
        or re.search(
            r"\b(?:add|define|require|requires|rename|renames)\s+"
            r"(?:a\s+|an\s+)?public\s+"
            r"(?:function|class|constant|api|symbol)\b",
            text,
        )
    )


def _line_has_dataclass_field_signal(line: str) -> bool:
    text = line.lower()
    return bool(
        re.search(r"\bexpected\s+dataclass\s+fields?\b", text)
        or re.search(r"\bdataclass\s+fields?\b", text)
    )


def _line_is_dotted_dataclass_field_item(line: str) -> bool:
    items = _backticked_items(line)
    if len(items) != 1:
        return False
    stripped = line.strip().lstrip("-* ").strip()
    if stripped.rstrip(".") != f"`{items[0]}`":
        return False
    match = _DOTTED_FIELD_RE.fullmatch(items[0])
    if not match:
        return False
    return match.group("class")[:1].isupper()


def _line_has_forbidden_call_signal(line: str) -> bool:
    text = line.lower()
    return bool(
        re.search(r"\bforbidden\s+calls?\b", text)
        or re.search(r"\bmust\s+not\s+call\b", text)
        or re.search(r"\bmust\s+not\s+use\b", text)
        or re.search(r"\bdo\s+not\s+(?:call|use)\b", text)
    )


def _line_has_forbidden_public_method_signal(line: str) -> bool:
    text = line.lower()
    return bool(
        re.search(r"\bforbidden\s+public\s+methods?\b", text)
        or re.search(
            r"\bmust\s+not\s+(?:add|introduce)\s+"
            r"(?:a\s+|an\s+)?public\s+method\b",
            text,
        )
        or re.search(
            r"\bdo\s+not\s+(?:add|introduce)\s+"
            r"(?:a\s+|an\s+)?public\s+method\b",
            text,
        )
        or re.search(r"\bno\s+public\s+method\b", text)
    )


def _is_identifier(value: str) -> bool:
    return bool(_IDENTIFIER_RE.fullmatch(value))


def _add_dataclass_field(
    fields: dict[str, list[str]],
    class_name: str,
    field_name: str,
) -> None:
    if not _is_identifier(class_name) or not _is_identifier(field_name):
        return
    fields.setdefault(class_name, []).append(field_name)


def _merge_list(existing: list[str], additions: list[str]) -> list[str]:
    return _dedupe([*existing, *additions])


def _merge_dataclass_fields(
    existing: dict[str, list[str]],
    additions: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = {class_name: _dedupe(list(fields)) for class_name, fields in existing.items()}
    for class_name, fields in additions.items():
        merged[class_name] = _dedupe([*merged.get(class_name, []), *fields])
    return merged


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = ["enrich_worker_dispatch_contract"]
