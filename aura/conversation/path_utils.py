"""Shared path normalization and validation-scratch helpers."""
from __future__ import annotations


def normalize_worker_path(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    return "/".join(parts)


def unique_worker_paths(paths: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = normalize_worker_path(path).strip()
        if not normalized or normalized in seen:
            continue
        unique.append(normalized)
        seen.add(normalized)
    return unique


def is_validation_scratch_path(path: str) -> bool:
    normalized = normalize_worker_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if normalized.startswith(".aura/tmp/") or "/" not in normalized:
        return name.startswith(
            (
                "dump",
                "_check",
                "check",
                "tmp",
                "_tmp",
                "_inspect",
                "inspect",
                "diagnostic",
                "_diagnostic",
            )
        )
    return False
