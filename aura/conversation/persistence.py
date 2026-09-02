"""Conversation persistence — JSON files in `<workspace>/.aura/conversations/`.

Schema:
- One-model conversation. {version, model, thinking, system_prompt,
  messages, chat_items, provider, ...}.

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
    DEFAULT_THINKING,
    ProviderId,
    ThinkingMode,
)
from aura.conversation.chat_transcript import (
    legacy_chat_items_from_messages,
    normalize_chat_items,
)
from aura.conversation.history import History
from aura.conversation.telemetry import ConversationTelemetry
from aura.git_ops import ensure_aura_gitignored
from aura.paths import safe_is_relative_to
from aura.providers.base import normalize_thinking_mode
from aura.settings import migrate_provider_and_model

SCHEMA_VERSION = 3
CONVERSATIONS_SUBDIR = ".aura/conversations"


@dataclass
class ConversationMeta:
    path: Path
    created_at: str
    title: str
    model: str
    thinking: ThinkingMode


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
        if msg.get("aura_internal"):
            continue
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
    chat_items: list[dict[str, Any]] | None = None,
    provider: ProviderId | None = None,
    telemetry: ConversationTelemetry | None = None,
) -> Path:
    """Write the conversation to disk and return the file path."""
    target_dir = conversations_dir(workspace_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    ensure_aura_gitignored(workspace_root)

    if existing_path is not None:
        if safe_is_relative_to(existing_path, target_dir):
            path = existing_path
        else:
            path = _new_path(target_dir, history, title)
    else:
        path = _new_path(target_dir, history, title)

    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "created_at": _read_created_at(path) or _utc_iso(),
        "model": model,
        "thinking": normalize_thinking_mode(thinking) or DEFAULT_THINKING,
        "system_prompt": history.system_prompt,
        "messages": copy.deepcopy(history.messages),
        "chat_items": normalize_chat_items(chat_items)
        if chat_items is not None
        else legacy_chat_items_from_messages(history.messages),
        "provider": provider or "deepseek",
        "telemetry": (telemetry or ConversationTelemetry()).to_dict(),
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
    chat_items: list[dict[str, Any]] = field(default_factory=list)
    telemetry: ConversationTelemetry = field(default_factory=ConversationTelemetry)


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

    if "chat_items" in data:
        chat_items = normalize_chat_items(data.get("chat_items"))
    else:
        chat_items = legacy_chat_items_from_messages(history.messages)

    # Legacy records containing "auto" are loaded as High; explicit modes are
    # kept unchanged.
    thinking = normalize_thinking_mode(data.get("thinking")) or DEFAULT_THINKING

    # Provider and model migrate as one unit, the same way saved settings do:
    # a record written against a removed provider is restored on
    # DEFAULT_PROVIDER with that provider's own default model, never with the
    # retired model id still attached. ``strict_model=False`` keeps the
    # permissive rule for surviving providers — any string is a valid model ID
    # here, because a conversation must reload with the model it actually ran
    # on even when the dynamic catalog no longer lists it. Files with no
    # provider field (v1/v2) default to DeepSeek.
    provider, model = migrate_provider_and_model(
        data.get("provider"),
        data.get("model"),
        strict_model=False,
    )

    return LoadedConversation(
        history=history,
        model=model,
        thinking=thinking,
        path=path,
        provider=provider,
        chat_items=chat_items,
        telemetry=ConversationTelemetry.from_dict(data.get("telemetry")),
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


# ---- helpers --------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _file_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
