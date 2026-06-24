"""Accessible-tree snapshot for UI selfcheck."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtGui import QAccessible

logger = logging.getLogger(__name__)

_MAX_DEPTH = 40
_MAX_NODES = 5000


def _role_name(interface: QAccessible) -> str:
    role = interface.role()
    name = role.name
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")
    return name


def _build_node(interface: QAccessible, depth: int, state: dict) -> dict:
    obj = interface.object()
    rect = interface.rect()
    node: dict = {
        "role": _role_name(interface),
        "name": interface.text(QAccessible.Text.Name),
        "object_name": obj.objectName() if obj is not None else "",
        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
        "children": [],
    }
    state["count"] += 1

    if state["count"] >= _MAX_NODES:
        state["truncated"] = True
        return node

    if depth >= _MAX_DEPTH:
        if interface.childCount() > 0:
            state["truncated"] = True
        return node

    child_count = interface.childCount()
    for i in range(child_count):
        child_iface = interface.child(i)
        if child_iface is not None:
            node["children"].append(_build_node(child_iface, depth + 1, state))
            if state["count"] >= _MAX_NODES:
                state["truncated"] = True
                break

    return node


def snapshot_widget_tree(root) -> dict:
    """Walk the QAccessible tree of *root* and return a JSON-serializable dict."""
    iface = QAccessible.queryAccessibleInterface(root)
    if iface is None:
        return {"schema_version": 1, "node_count": 0, "truncated": False, "root": None}

    state: dict = {"count": 0, "truncated": False}
    root_node = _build_node(iface, 0, state)

    return {
        "schema_version": 1,
        "node_count": state["count"],
        "truncated": state["truncated"],
        "root": root_node,
    }


def dump_ui_tree(root, path: str | Path) -> bool:
    """Snapshot the widget tree and write JSON to *path*. Returns True on success."""
    try:
        data = snapshot_widget_tree(root)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
        return True
    except Exception:
        logger.exception("dump-ui-tree failed")
        return False
