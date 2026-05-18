"""Conversation persistence — JSON files in `<workspace>/.aura/conversations/`.

Schema:
- v1 (legacy): single-model conversation. {version, model, thinking,
  system_prompt, messages, ...}.
- v2: planner-worker aware. Adds planner_worker_mode, planner_model,
  worker_model, planner_thinking, worker_thinking, and an optional
  worker_dispatches list. Loading v1 is backward-compatible: it's treated as
  planner_worker_mode=False with a single set of messages on the planner.
- v2 + provider: v2 schema extended with a `provider` field.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aura.config import (
    DEFAULT_MODEL,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_PLANNER_THINKING,
    DEFAULT_THINKING,
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    ProviderId,
    ThinkingMode,
)
from aura.conversation.history import History
from aura.git_ops import ensure_aura_gitignored
from aura.providers.registry import provider_registry

SCHEMA_VERSION = 2
CONVERSATIONS_SUBDIR = ".aura/conversations"


@dataclass
class ConversationMeta:
    path: Path
    created_at: str
    title: str
    model: str
    thinking: ThinkingMode


@dataclass
class WorkerDispatchRecord:
    """One worker dispatch fired during a planner conversation. Stored
    alongside the planner history so the chat can be replayed faithfully.
    """
    after_message_index: int
    spec: dict[str, Any]
    worker_history: list[dict[str, Any]]
    result_summary: str
    tool_call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "after_message_index": self.after_message_index,
            "tool_call_id": self.tool_call_id,
            "spec": dict(self.spec),
            "worker_history": list(self.worker_history),
            "result_summary": self.result_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerDispatchRecord":
        return cls(
            after_message_index=int(data.get("after_message_index", 0)),
            tool_call_id=str(data.get("tool_call_id", "")),
            spec=dict(data.get("spec") or {}),
            worker_history=list(data.get("worker_history") or []),
            result_summary=str(data.get("result_summary", "")),
        )


def conversations_dir(workspace_root: Path) -> Path:
    return workspace_root / CONVERSATIONS_SUBDIR


def _slugify(text: str, max_len: int = 40) -> str:
    if not text:
        return "untitled"
    words = text.strip().split()[:6]
    s = "-".join(words).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def _first_user_text(history: History) -> str:
    for msg in history.messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return str(part.get("text", ""))
    return ""


def save_conversation(
    history: History,
    workspace_root: Path,
    model: str,
    thinking: ThinkingMode,
    *,
    title: str | None = None,
    existing_path: Path | None = None,
    planner_worker_mode: bool = False,
    planner_model: str | None = None,
    worker_model: str | None = None,
    planner_thinking: ThinkingMode | None = None,
    worker_thinking: ThinkingMode | None = None,
    worker_dispatches: list[WorkerDispatchRecord] | None = None,
    provider: ProviderId | None = None,
    planner_provider: ProviderId | None = None,
    worker_provider: ProviderId | None = None,
) -> Path:
    """Write the conversation to disk and return the file path."""
    target_dir = conversations_dir(workspace_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    ensure_aura_gitignored(workspace_root)

    if existing_path is not None:
        try:
            existing_path.resolve().relative_to(target_dir.resolve())
            path = existing_path
        except ValueError:
            path = _new_path(target_dir, history, title)
    else:
        path = _new_path(target_dir, history, title)

    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "created_at": _read_created_at(path) or _utc_iso(),
        "model": model,
        "thinking": thinking,
        "planner_worker_mode": bool(planner_worker_mode),
        "planner_model": planner_model or model,
        "worker_model": worker_model or DEFAULT_WORKER_MODEL,
        "planner_thinking": planner_thinking or thinking,
        "worker_thinking": worker_thinking or DEFAULT_WORKER_THINKING,
        "system_prompt": history.system_prompt,
        "messages": copy.deepcopy(history.messages),
        "worker_dispatches": [
            d.to_dict() for d in (worker_dispatches or [])
        ],
        "provider": provider or "deepseek",
        "planner_provider": planner_provider or provider or "deepseek",
        "worker_provider": worker_provider or provider or "deepseek",
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _new_path(target_dir: Path, history: History, title: str | None) -> Path:
    ts = _file_timestamp()
    slug = _slugify(title if title is not None else _first_user_text(history))
    return target_dir / f"{ts}-{slug}.json"


def _read_created_at(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("created_at") if isinstance(data, dict) else None
    return val if isinstance(val, str) else None


@dataclass
class LoadedConversation:
    history: History
    model: str
    thinking: ThinkingMode
    path: Path
    provider: ProviderId = "deepseek"
    planner_provider: ProviderId = "deepseek"
    worker_provider: ProviderId = "deepseek"
    planner_worker_mode: bool = False
    planner_model: str = DEFAULT_PLANNER_MODEL
    worker_model: str = DEFAULT_WORKER_MODEL
    planner_thinking: ThinkingMode = DEFAULT_PLANNER_THINKING
    worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
    worker_dispatches: list[WorkerDispatchRecord] = field(default_factory=list)


def load_conversation(path: Path) -> LoadedConversation:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Conversation file is not a JSON object: {path}")

    history = History()
    sp = data.get("system_prompt")
    if isinstance(sp, str):
        history.set_system(sp)
    msgs = data.get("messages")
    if isinstance(msgs, list):
        history.messages = [m for m in msgs if isinstance(m, dict)]

    # Any string is now valid as a model ID — no hardcoded valid_models list.
    valid_thinking = ("off", "high", "max")

    model = data.get("model") if isinstance(data.get("model"), str) else DEFAULT_MODEL
    thinking = data.get("thinking") if data.get("thinking") in valid_thinking else DEFAULT_THINKING

    # Provider: default to "deepseek" for backward compat with v1/v2 files.
    provider_raw = data.get("provider")
    provider: ProviderId = "deepseek"
    if isinstance(provider_raw, str) and provider_registry.has(provider_raw):
        provider = provider_raw  # type: ignore[assignment]

    planner_provider_raw = data.get("planner_provider")
    planner_provider: ProviderId = provider
    if isinstance(planner_provider_raw, str) and provider_registry.has(planner_provider_raw):
        planner_provider = planner_provider_raw  # type: ignore[assignment]

    worker_provider_raw = data.get("worker_provider")
    worker_provider: ProviderId = provider
    if isinstance(worker_provider_raw, str) and provider_registry.has(worker_provider_raw):
        worker_provider = worker_provider_raw  # type: ignore[assignment]

    version = data.get("version")
    if version == 2:
        pwm = bool(data.get("planner_worker_mode", False))
        planner_model = data.get("planner_model") if isinstance(data.get("planner_model"), str) else model
        worker_model = data.get("worker_model") if isinstance(data.get("worker_model"), str) else DEFAULT_WORKER_MODEL
        planner_thinking = data.get("planner_thinking") if data.get("planner_thinking") in valid_thinking else thinking
        worker_thinking = data.get("worker_thinking") if data.get("worker_thinking") in valid_thinking else DEFAULT_WORKER_THINKING
        raw_dispatches = data.get("worker_dispatches") or []
        dispatches = [
            WorkerDispatchRecord.from_dict(d)
            for d in raw_dispatches
            if isinstance(d, dict)
        ]
    else:
        # v1 (or unversioned): treat as single-model.
        pwm = False
        planner_model = model
        worker_model = DEFAULT_WORKER_MODEL
        planner_thinking = thinking
        worker_thinking = DEFAULT_WORKER_THINKING
        dispatches = []

    return LoadedConversation(
        history=history,
        model=model,
        thinking=thinking,
        path=path,
        provider=provider,
        planner_worker_mode=pwm,
        planner_model=planner_model,
        worker_model=worker_model,
        planner_thinking=planner_thinking,
        worker_thinking=worker_thinking,
        worker_dispatches=dispatches,
    )


def list_conversations(workspace_root: Path) -> list[Path]:
    target_dir = conversations_dir(workspace_root)
    if not target_dir.is_dir():
        return []
    # Faster stat-based sort using os.scandir
    import os
    files = []
    try:
        for entry in os.scandir(str(target_dir)):
            if entry.is_file() and entry.name.endswith(".json"):
                files.append((entry.path, entry.stat().st_mtime))
    except OSError:
        return []
    
    files.sort(key=lambda x: x[1], reverse=True)
    return [Path(f[0]) for f in files]


def most_recent_conversation(workspace_root: Path) -> Path | None:
    files = list_conversations(workspace_root)
    return files[0] if files else None


def save_dispatch_record_to_memory(record: WorkerDispatchRecord, workspace_root: Path) -> int | None:
    """Persist a ``WorkerDispatchRecord`` into the project memory DB.

    This is called automatically after a dispatch completes so that past
    dispatch records are available via the ``search_project_memory`` tool.

    Args:
        record: The completed dispatch record.
        workspace_root: The workspace root (used to locate ``.aura/memory.db``).

    Returns:
        The inserted memory ID, or ``None`` if the insert failed.
    """
    try:
        from aura.memory_db import ProjectMemoryDB

        spec = record.spec or {}
        goal = spec.get("goal", "")
        acceptance = spec.get("acceptance", "")
        files_list = spec.get("files", [])

        content = (
            f"Goal: {goal}\n"
            f"Spec: {spec}\n"
            f"Acceptance: {acceptance}\n"
            f"Files: {', '.join(files_list) if isinstance(files_list, list) else files_list}\n"
            f"Outcome: {record.result_summary or 'N/A'}"
        )
        metadata: dict[str, object] = {
            "type": "dispatch_record",
            "goal": str(goal)[:100],
            "timestamp": _utc_iso(),
        }
        db = ProjectMemoryDB(workspace_root / ".aura" / "memory.db")
        return db.insert(content, metadata)
    except Exception:
        return None


# ---- helpers --------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _file_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
