"""Tests for ToolStreamController — streaming JSON argument extraction."""

from __future__ import annotations

import pytest
from aura.gui.controllers import ToolStreamController


def test_path_extraction():
    controller = ToolStreamController("write_file")
    resolved_paths = []
    controller.path_resolved.connect(resolved_paths.append)

    controller.append_fragment('{"path": "src/main.py", "content": "hello"}')
    assert resolved_paths == ["src/main.py"]


def test_streaming_content_write_file():
    controller = ToolStreamController("write_file")
    updates = []
    controller.content_updated.connect(updates.append)

    controller.append_fragment('{"path": "test.py", "content": "line 1')
    assert updates[-1] == "line 1"

    controller.append_fragment('\\nline 2')
    assert updates[-1] == "line 1\nline 2"

    controller.append_fragment('"}')
    assert updates[-1] == "line 1\nline 2"


def test_streaming_content_dispatch_to_worker():
    controller = ToolStreamController("dispatch_to_worker")
    updates = []
    controller.content_updated.connect(updates.append)

    controller.append_fragment('{"goal": "Fix bug", "spec": "Step 1')
    assert updates[-1] == "Step 1"

    controller.append_fragment(': Do X')
    assert updates[-1] == "Step 1: Do X"

    # Verify it strips trailing JSON
    controller.append_fragment('", "acceptance": "Done"}')
    assert updates[-1] == "Step 1: Do X"


def test_streaming_goal():
    controller = ToolStreamController("dispatch_to_worker")
    goal_updates = []
    controller.goal_updated.connect(goal_updates.append)

    controller.append_fragment('{"goal": "Refactor auth')
    assert goal_updates[-1] == "Refactor auth"

    controller.append_fragment(' logic", "spec": "..."}')
    assert goal_updates[-1] == "Refactor auth logic"


def test_run_research_streaming():
    controller = ToolStreamController("run_research")
    content_updates = []
    goal_updates = []
    controller.content_updated.connect(content_updates.append)
    controller.goal_updated.connect(goal_updates.append)

    controller.append_fragment('{"objective": "How to use PySide6?')
    assert content_updates[-1] == "How to use PySide6?"
    # For research, objective is also the goal
    assert goal_updates[-1] == "How to use PySide6?"


def test_escape_handling():
    controller = ToolStreamController("write_file")
    updates = []
    controller.content_updated.connect(updates.append)

    controller.append_fragment('{"content": "line 1\\nline 2\\tback\\\\slash\\"quote"}')
    # Final parse should be correct
    assert updates[-1] == 'line 1\nline 2\tback\\slash"quote'

def test_partial_escape_streaming():
    controller = ToolStreamController("write_file")
    updates = []
    controller.content_updated.connect(updates.append)

    controller.append_fragment('{"content": "line 1\\')
    # The backslash is pending, so it might show up as 'line 1' (escaped is True)
    assert updates[-1] == 'line 1'
    
    controller.append_fragment('nline 2"}')
    assert updates[-1] == 'line 1\nline 2'
