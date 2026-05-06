import os
import sys
import threading
from pathlib import Path

# Force stdout to UTF-8
sys.stdout.reconfigure(encoding='utf-8')

from aura.client import DeepSeekClient, ToolResult
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.registry import ToolRegistry, ApprovalDecision

def main():
    client = DeepSeekClient()
    history = History()
    history.set_system("You are a planner. You have access to the run_research tool. You MUST use run_research. DO NOT use dispatch_to_worker.")
    registry = ToolRegistry(Path.cwd(), mode="planner")
    manager = ConversationManager(client, history, registry)
    
    history.append_user_text("Call the run_research tool to find out the latest features of Python 3.13. Pass the 'objective' argument as 'latest features of Python 3.13'.")
    
    def on_event(ev):
        if isinstance(ev, ToolResult):
            print(f"\nTOOL RESULT [{ev.name}]: ok={ev.ok}")
            print(ev.result[:500] + ("..." if len(ev.result) > 500 else ""))
        else:
            name = type(ev).__name__
            if name not in ("ContentDelta", "ReasoningDelta"):
                print(f"EVENT: {name}")
        
    print("Sending...")
    manager.send(
        on_event=on_event,
        approval_cb=lambda r: ApprovalDecision(action="reject"),
        cancel_event=threading.Event(),
        model="deepseek-v4-flash",
        thinking="off"
    )
    print("\nDone.")

if __name__ == "__main__":
    main()
