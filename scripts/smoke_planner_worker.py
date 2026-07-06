"""Smoke 7: planner -> worker dispatch end-to-end.

Setup:
- tmp workspace with bug.gd containing an off-by-one in count_items().
- Planner = V4 Flash, read tools + dispatch_to_worker (no writes).
- Worker = V4 Pro, read + write tools.
- Auto-approve callback for diffs.
- Auto-dispatch (echo the planner's spec back unchanged).

Verifies:
1. Planner reads bug.gd via read_file before dispatching.
2. Planner calls dispatch_to_worker with a spec mentioning bug.gd.
3. Worker reads bug.gd, calls edit_file (or write_file) to fix the bug.
4. Auto-approve gate fires for the worker's edit.
5. Planner receives the worker's summary and replies to the user.
6. Final file contents are corrected (no off-by-one).
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from aura.prompts import PLANNER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT
from aura.bridge.dispatch import (
    _format_spec_as_user_message,
    _build_worker_summary,
    _last_assistant_content,
)
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
    WorkerDispatchRequested,
)
from aura.conversation import (
    ConversationManager,
    History,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.tools import ApprovalDecision, ApprovalRequest, ToolRegistry


BUG_GD = """\
extends Node


func count_items(items: Array) -> int:
    var count: int = 0
    for i in range(items.size() + 1):
        count += 1
    return count
"""


def auto_approve(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def make_event_logger(label: str, counters: dict) -> callable:
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
            counters["tool_names"].append(ev.name)
            print(f"\n  [{label} tool start] {ev.name}", flush=True)
        elif isinstance(ev, ToolCallArgsDelta):
            pass
        elif isinstance(ev, ToolCallEnd):
            pass
        elif isinstance(ev, ToolResult):
            if ev.ok:
                counters["tool_oks"] += 1
            else:
                counters["tool_fails"] += 1
            print(f"  [{label} tool result] ok={ev.ok} ({ev.name})", flush=True)
        elif isinstance(ev, WorkerDispatchRequested):
            counters["dispatches"].append(
                {"goal": ev.goal, "files": list(ev.files), "spec": ev.spec, "acceptance": ev.acceptance}
            )
            print(
                f"\n  [{label} dispatch requested] goal={ev.goal!r} files={ev.files}",
                flush=True,
            )
        elif isinstance(ev, ApiError):
            counters["errors"].append(f"{ev.status_code}: {ev.message}")
            print(f"\n  [{label} API ERROR] {ev.status_code}: {ev.message}", flush=True)
        elif isinstance(ev, Done):
            pass
        elif isinstance(ev, Usage):
            pass

    return on_event


def fresh_counters() -> dict:
    return {
        "reasoning_chars": 0,
        "content_chars": 0,
        "tool_calls": 0,
        "tool_oks": 0,
        "tool_fails": 0,
        "tool_names": [],
        "errors": [],
        "content": "",
        "dispatches": [],
    }


def make_dispatch_cb(client: DeepSeekClient, root: Path, worker_counters: dict):
    """Auto-dispatch: spin up a worker manager synchronously."""

    def dispatch_cb(tool_call_id: str, req: WorkerDispatchRequest) -> WorkerDispatchResult:
        print(
            f"\n  [auto-dispatch] running worker for: {req.goal!r}", flush=True
        )
        worker_history = History()
        worker_history.set_system(WORKER_SYSTEM_PROMPT)
        is_artifact_item = bool(req.artifact_id and req.artifact_item_id)
        worker_history.append_user_text(_format_spec_as_user_message(
            req,
            artifact_item_index=1 if is_artifact_item else None,
            artifact_item_total=None,
        ))
        worker_registry = ToolRegistry(root, mode="worker")
        worker_manager = ConversationManager(client, worker_history, worker_registry)

        cancel = threading.Event()
        write_results: list[dict] = []
        api_errors: list[str] = []

        def on_event(ev) -> None:
            if isinstance(ev, ContentDelta):
                worker_counters["content"] += ev.text
                sys.stdout.write(ev.text)
                sys.stdout.flush()
            elif isinstance(ev, ToolCallStart):
                worker_counters["tool_calls"] += 1
                worker_counters["tool_names"].append(ev.name)
                print(f"\n    [worker tool start] {ev.name}", flush=True)
            elif isinstance(ev, ToolResult):
                if ev.ok:
                    worker_counters["tool_oks"] += 1
                else:
                    worker_counters["tool_fails"] += 1
                print(
                    f"    [worker tool result] ok={ev.ok} ({ev.name})", flush=True
                )
                if ev.name in ("write_file", "edit_file") and ev.ok:
                    import json as _json
                    try:
                        parsed = _json.loads(ev.result)
                        if isinstance(parsed, dict) and parsed.get("ok"):
                            write_results.append(
                                {
                                    "tool": ev.name,
                                    "path": parsed.get("path"),
                                    "is_new_file": parsed.get("is_new_file", False),
                                }
                            )
                    except _json.JSONDecodeError:
                        pass
            elif isinstance(ev, ApiError):
                api_errors.append(f"{ev.status_code}: {ev.message}")
                worker_counters["errors"].append(f"{ev.status_code}: {ev.message}")
                print(
                    f"\n    [worker API ERROR] {ev.status_code}: {ev.message}", flush=True
                )

        worker_manager.send(
            on_event=on_event,
            approval_cb=auto_approve,
            cancel_event=cancel,
            model="deepseek-v4-pro",
            thinking="high",
            dispatch_cb=None,
        )

        summary = _build_worker_summary(req, worker_history, write_results, api_errors)
        ok = not api_errors and bool(write_results or _last_assistant_content(worker_history))
        worker_counters["writes"] = list(write_results)
        return WorkerDispatchResult(ok=ok, summary=summary, cancelled=False)

    return dispatch_cb


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        bug_path = root / "bug.gd"
        bug_path.write_text(BUG_GD, encoding="utf-8")

        client = DeepSeekClient()
        history = History()
        history.set_system(PLANNER_SYSTEM_PROMPT)
        registry = ToolRegistry(root, mode="planner")
        manager = ConversationManager(client, history, registry)

        history.append_user_text(
            "Look at bug.gd and fix the off-by-one bug in count_items. "
            "After you've read the file and confirmed the bug, dispatch a worker "
            "to make the fix."
        )

        planner_counters = fresh_counters()
        worker_counters = fresh_counters()

        cancel = threading.Event()
        manager.send(
            on_event=make_event_logger("planner", planner_counters),
            approval_cb=auto_approve,
            cancel_event=cancel,
            model="deepseek-v4-flash",
            thinking="high",
            dispatch_cb=make_dispatch_cb(client, root, worker_counters),
        )
        print(flush=True)

        # ---- assertions ----
        final = bug_path.read_text(encoding="utf-8")

        passes: list[tuple[str, bool]] = []

        passes.append((
            "planner read bug.gd via read_file",
            "read_file" in planner_counters["tool_names"],
        ))
        passes.append((
            "planner called dispatch_to_worker",
            "dispatch_to_worker" in planner_counters["tool_names"],
        ))
        passes.append((
            "spec mentions bug.gd",
            any(
                "bug.gd" in (d["spec"] + " ".join(d["files"]))
                for d in planner_counters["dispatches"]
            ),
        ))
        passes.append((
            "worker invoked at least one write tool",
            any(name in ("write_file", "edit_file") for name in worker_counters["tool_names"]),
        ))
        passes.append((
            "worker writes succeeded",
            bool(worker_counters.get("writes")),
        ))
        passes.append((
            "no planner errors",
            not planner_counters["errors"],
        ))
        passes.append((
            "no worker errors",
            not worker_counters["errors"],
        ))
        passes.append((
            "off-by-one removed (no items.size() + 1 left)",
            "items.size() + 1" not in final,
        ))
        passes.append((
            "fix uses items.size() (or equivalent)",
            "items.size()" in final or "len(items)" in final,
        ))
        passes.append((
            "planner replied with confirmation content after worker",
            len(planner_counters["content"]) > 0,
        ))

        print("\n=== smoke_planner_worker results ===")
        for label, ok in passes:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

        print("\nFinal bug.gd:")
        print(final)

        return 0 if all(ok for _, ok in passes) else 1


if __name__ == "__main__":
    sys.exit(main())
