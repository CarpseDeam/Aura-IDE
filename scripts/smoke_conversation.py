"""Smoke 4: full tool-loop with auto-approve and multi-turn continuation.

Setup: tmp workspace with a README.md. Ask the model to read it and answer.
Then a follow-up that should trigger another tool call — verifying the
stateless continuation projection does not replay prior reasoning content.
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
from pathlib import Path

# Make stdout UTF-8 so model output (emojis, accents) doesn't crash on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from aura.client import (
    ApiError,
    ContentDelta,
    DeepSeekClient,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
)
from aura.conversation import ConversationManager, History
from aura.conversation.tools import ApprovalDecision, ApprovalRequest, ToolRegistry
from aura.prompts import SINGLE_SYSTEM_PROMPT


def auto_approve(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def run_turn(label: str, manager: ConversationManager, model: str, thinking: str) -> dict:
    print(f"\n=== {label} ===", flush=True)
    cancel = threading.Event()
    counters = {
        "reasoning_chars": 0,
        "content_chars": 0,
        "tool_calls": 0,
        "tool_oks": 0,
        "errors": [],
        "content": "",
    }

    def on_event(ev) -> None:
        if isinstance(ev, ReasoningDelta):
            counters["reasoning_chars"] += len(ev.text)
        elif isinstance(ev, ContentDelta):
            counters["content_chars"] += len(ev.text)
            counters["content"] += ev.text
            sys.stdout.write(ev.text)
            sys.stdout.flush()
        elif isinstance(ev, ToolCallStart):
            counters["tool_calls"] += 1
            print(f"\n  [tool start] {ev.name} (id={ev.id})")
        elif isinstance(ev, ToolCallArgsDelta):
            pass  # Quiet
        elif isinstance(ev, ToolCallEnd):
            pass
        elif isinstance(ev, ToolResult):
            if ev.ok:
                counters["tool_oks"] += 1
            print(f"  [tool result] ok={ev.ok} ({ev.name})")
        elif isinstance(ev, Done):
            pass
        elif isinstance(ev, Usage):
            pass
        elif isinstance(ev, ApiError):
            counters["errors"].append(f"{ev.status_code}: {ev.message}")
            print(f"\n  [API ERROR] {ev.status_code}: {ev.message}")

    manager.send(
        on_event=on_event,
        approval_cb=auto_approve,
        cancel_event=cancel,
        model=model,
        thinking=thinking,
    )
    print()
    return counters


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        (root / "README.md").write_text(
            "Aura is a desktop chat troubleshooter for code, powered by DeepSeek V4.\n"
            "Magic word: SQUIRREL.\n",
            encoding="utf-8",
        )
        (root / "main.py").write_text("print('hi')\n", encoding="utf-8")

        client = DeepSeekClient()
        history = History()
        history.set_system(SINGLE_SYSTEM_PROMPT)
        tools = ToolRegistry(root, read_only=False)
        manager = ConversationManager(client=client, history=history, tool_registry=tools)

        # Turn 1: should call read_file.
        history.append_user_text(
            "Read README.md from the workspace and tell me the magic word it mentions."
        )
        c1 = run_turn("turn 1: trigger tool call", manager, "deepseek-v4-flash", "high")
        ok1 = (
            c1["tool_calls"] >= 1
            and c1["tool_oks"] >= 1
            and not c1["errors"]
            and "SQUIRREL" in c1["content"].upper()
        )
        print(
            f"\nturn 1: tool_calls={c1['tool_calls']} tool_oks={c1['tool_oks']} "
            f"errors={c1['errors']} mentions_squirrel={'SQUIRREL' in c1['content'].upper()}"
        )

        # Turn 2: triggers another tool call AFTER a tool-call assistant message
        # exists in canonical history — exercises stateless multi-round projection.
        history.append_user_text(
            "Now list the files in the workspace root, and tell me whether main.py is among them."
        )
        c2 = run_turn("turn 2: replay-rule test", manager, "deepseek-v4-flash", "high")
        ok2 = (
            c2["tool_calls"] >= 1
            and c2["tool_oks"] >= 1
            and not c2["errors"]
            and "main.py" in c2["content"].lower()
        )
        print(
            f"\nturn 2: tool_calls={c2['tool_calls']} tool_oks={c2['tool_oks']} "
            f"errors={c2['errors']} mentions_main_py={'main.py' in c2['content'].lower()}"
        )

        return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
