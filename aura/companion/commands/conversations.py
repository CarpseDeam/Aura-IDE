"""Companion command handlers: conversation.list, conversation.select, conversation.history."""
from __future__ import annotations

import logging

from aura.companion.commands import CommandContext
from aura.companion.protocol import CompanionThread
from aura.companion.replies import build_reply_envelope
from aura.conversation.history import History
from aura.conversation.persistence import load_conversation, save_conversation
from aura.projects.store import ProjectStore

logger = logging.getLogger(__name__)


def handle_conversation_list(msg: dict, ctx: CommandContext) -> None:
    """List threads for the current project, or a specified project."""
    payload = msg.get("payload", {})
    project_id = payload.get("project_id", ctx.state.current_project_id)
    if not project_id or not ctx.state.workspace_root:
        env = build_reply_envelope(msg, "conversation.list_result", {"threads": []})
        if env:
            ctx.send_fn(env)
        return
    try:
        store = ProjectStore()
        project = store.load_project(project_id)
        if not project:
            env = build_reply_envelope(msg, "conversation.list_result", {"threads": [], "error": "Project not found"})
            if env:
                ctx.send_fn(env)
            return
        threads = store.list_threads(project)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        dtos = []
        for t in threads[:50]:
            dtos.append(CompanionThread(
                id=t.id,
                title=t.title or "Untitled",
                updated_at=t.updated_at,
                is_current=(t.id == ctx.state.current_conversation_id),
            ).to_dict())
        env = build_reply_envelope(msg, "conversation.list_result", {"threads": dtos})
        if env:
            ctx.send_fn(env)
    except Exception as exc:
        logger.error("[Companion] conversation.list error: %s", exc)
        env = build_reply_envelope(msg, "conversation.list_result", {"threads": [], "error": str(exc)})
        if env:
            ctx.send_fn(env)


def handle_conversation_select(msg: dict, ctx: CommandContext) -> None:
    """Select a thread as the active conversation."""
    payload = msg.get("payload", {})
    thread_id = payload.get("thread_id", "")
    project_id = payload.get("project_id", ctx.state.current_project_id)
    if not thread_id or not project_id:
        env = build_reply_envelope(msg, "conversation.selected", {"error": "Missing thread_id or project_id"})
        if env:
            ctx.send_fn(env)
        return
    try:
        store = ProjectStore()
        project = store.load_project(project_id)
        if not project:
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Project not found"})
            if env:
                ctx.send_fn(env)
            return
        thread = store.load_thread(project, thread_id)
        if not thread:
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Thread not found"})
            if env:
                ctx.send_fn(env)
            return
        if thread.conversation_path is None:
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Thread has no conversation file"})
            if env:
                ctx.send_fn(env)
            return
        if ctx.bridge is not None and ctx.bridge.is_running():
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Desktop is busy"})
            if env:
                ctx.send_fn(env)
            return
        ctx.state.pending_select_msg = msg
        if ctx.on_conversation_selected is not None:
            ctx.on_conversation_selected(project.root_path, thread.conversation_path)
    except Exception as exc:
        logger.error("[Companion] conversation.select error: %s", exc)
        env = build_reply_envelope(msg, "conversation.selected", {"error": str(exc)})
        if env:
            ctx.send_fn(env)


def handle_conversation_create(msg: dict, ctx: CommandContext) -> None:
    """Create a new empty conversation thread on the phone."""
    payload = msg.get("payload", {})
    project_id = payload.get("project_id", "")
    if not project_id:
        env = build_reply_envelope(msg, "conversation.selected", {"error": "Missing project_id"})
        if env:
            ctx.send_fn(env)
        return
    try:
        store = ProjectStore()
        project = store.load_project(project_id)
        if not project:
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Project not found"})
            if env:
                ctx.send_fn(env)
            return
        if ctx.bridge is not None and ctx.bridge.is_running():
            env = build_reply_envelope(msg, "conversation.selected", {"error": "Desktop is busy"})
            if env:
                ctx.send_fn(env)
            return

        settings = ctx.settings
        conv_path = save_conversation(
            History(),
            project.root_path,
            model=settings.default_model,
            thinking=settings.default_thinking,
            title="New chat",
            provider=settings.provider,
            planner_provider=settings.planner_provider,
            worker_provider=settings.worker_provider,
            planner_model=settings.default_planner_model,
            planner_thinking=settings.default_planner_thinking,
            worker_model=settings.default_worker_model,
            worker_thinking=settings.default_worker_thinking,
            planner_worker_mode=settings.planner_worker_mode,
        )

        thread = store.create_thread(project, title="New chat")
        thread.conversation_path = conv_path
        store.save_thread(project, thread)

        project.last_thread_id = thread.id
        store.save_project(project)

        ctx.state.pending_select_msg = {
            "id": msg.get("id", ""),
            "sender_device_id": msg.get("sender_device_id", ""),
            "payload": {
                "project_id": project_id,
                "thread_id": thread.id,
            },
        }

        if ctx.on_conversation_selected is not None:
            ctx.on_conversation_selected(project.root_path, conv_path)
    except Exception as exc:
        logger.error("[Companion] conversation.create error: %s", exc)
        env = build_reply_envelope(msg, "conversation.selected", {"error": str(exc)})
        if env:
            ctx.send_fn(env)


def handle_conversation_history(msg: dict, ctx: CommandContext) -> None:
    """Return conversation history for a thread."""
    payload = msg.get("payload", {})
    project_id = payload.get("project_id") or ctx.state.current_project_id
    thread_id = payload.get("thread_id") or ctx.state.current_conversation_id
    if not project_id or not thread_id:
        env = build_reply_envelope(msg, "conversation.history_result", {
            "project_id": project_id,
            "thread_id": thread_id,
            "messages": [],
            "error": "Missing project_id or thread_id",
        })
        if env:
            ctx.send_fn(env)
        return
    try:
        if (
            ctx.state.conversation_loaded
            and thread_id == ctx.state.current_conversation_id
            and ctx.bridge is not None
        ):
            raw_messages = ctx.bridge.history.messages
        else:
            store = ProjectStore()
            project = store.load_project(project_id)
            if not project:
                env = build_reply_envelope(msg, "conversation.history_result", {
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "messages": [],
                    "error": "Project not found",
                })
                if env:
                    ctx.send_fn(env)
                return
            thread = store.load_thread(project, thread_id)
            if not thread or thread.conversation_path is None:
                env = build_reply_envelope(msg, "conversation.history_result", {
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "messages": [],
                    "error": "Thread or conversation file not found",
                })
                if env:
                    ctx.send_fn(env)
                return
            loaded = load_conversation(thread.conversation_path)
            raw_messages = loaded.history.messages

        mobile_messages: list[dict] = []
        for m in raw_messages:
            role = m.get("role", "")
            if role in ("tool", "system"):
                continue
            content = m.get("content")
            if role == "user":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    text = " ".join(parts)
                else:
                    continue
                if not text:
                    continue
                mobile_messages.append({"role": role, "content": text})
            elif role == "assistant":
                if not isinstance(content, str) or not content:
                    continue
                mobile_messages.append({"role": role, "content": content})

        env = build_reply_envelope(msg, "conversation.history_result", {
            "project_id": project_id,
            "thread_id": thread_id,
            "messages": mobile_messages,
        })
        if env:
            ctx.send_fn(env)
    except Exception as exc:
        logger.error("[Companion] conversation.history error: %s", exc)
        env = build_reply_envelope(msg, "conversation.history_result", {
            "project_id": project_id,
            "thread_id": thread_id,
            "messages": [],
            "error": str(exc),
        })
        if env:
            ctx.send_fn(env)
