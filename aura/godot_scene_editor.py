"""Small, deterministic edits for Godot text scenes.

This module deliberately handles only node sections and their properties.  It
does not try to become a second Godot editor or rewrite an entire scene model.
Keeping the transform pure makes it easy to test and lets the normal Aura write
layer own approval, backups, and atomic file replacement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_NODE_SECTION_RE = re.compile(r"^node(?:\s|$)")
_ATTRIBUTE_RE = re.compile(r'([A-Za-z_][\w]*)="((?:\\.|[^"\\])*)"')
_PROPERTY_RE = re.compile(r"^([A-Za-z_][\w/.:]*)\s*=\s*(.*)$")
_VALID_NODE_NAME_RE = re.compile(r"^[^/\[\]\r\n]+$")
_VALID_PROPERTY_RE = re.compile(r"^[A-Za-z_][\w/.:]*$")


@dataclass(frozen=True)
class SceneSection:
    start: int
    end: int
    header: str
    kind: str
    attributes: dict[str, str]


@dataclass(frozen=True)
class GodotSceneEditResult:
    content: str
    operations: tuple[str, ...]


class GodotSceneEditError(ValueError):
    """Raised when a requested scene edit is ambiguous or unsafe."""


def edit_godot_scene(content: str, operations: Iterable[dict[str, Any]]) -> GodotSceneEditResult:
    """Apply node-level operations to a Godot ``.tscn`` document.

    Supported operations are ``add_node``, ``remove_node``, ``set_property``,
    and ``remove_property``.  Node paths are relative to the scene root: ``.``
    names the root and ``Player/Sprite`` names a descendant.
    """
    if not isinstance(content, str) or not content.strip():
        raise GodotSceneEditError("scene is empty")
    if not content.lstrip().startswith("[gd_scene"):
        raise GodotSceneEditError("file is not a Godot text scene (.tscn)")

    requested = list(operations)
    if not requested:
        raise GodotSceneEditError("operations must contain at least one edit")

    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    summaries: list[str] = []
    for index, operation in enumerate(requested, start=1):
        if not isinstance(operation, dict):
            raise GodotSceneEditError(f"operation {index} must be an object")
        action = str(operation.get("action") or "").strip()
        if action == "add_node":
            lines, summary = _add_node(lines, operation)
        elif action == "remove_node":
            lines, summary = _remove_node(lines, operation)
        elif action == "set_property":
            lines, summary = _set_property(lines, operation)
        elif action == "remove_property":
            lines, summary = _remove_property(lines, operation)
        else:
            raise GodotSceneEditError(
                f"operation {index} has unsupported action {action!r}"
            )
        summaries.append(summary)

    edited = newline.join(lines)
    if content.endswith(("\n", "\r")):
        edited += newline
    return GodotSceneEditResult(content=edited, operations=tuple(summaries))


def scene_node_paths(content: str) -> list[str]:
    """Return the canonical root-relative node paths in a text scene."""
    return list(_node_sections(content.splitlines()))


def _sections(lines: list[str]) -> list[SceneSection]:
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            starts.append((index, match.group(1)))

    result: list[SceneSection] = []
    for position, (start, header) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        kind = header.split(None, 1)[0]
        attributes = {key: _unescape(value) for key, value in _ATTRIBUTE_RE.findall(header)}
        result.append(SceneSection(start, end, header, kind, attributes))
    return result


def _node_sections(lines: list[str]) -> dict[str, SceneSection]:
    nodes: dict[str, SceneSection] = {}
    for section in _sections(lines):
        if not _NODE_SECTION_RE.match(section.header):
            continue
        name = section.attributes.get("name")
        if not name:
            raise GodotSceneEditError(f"node section on line {section.start + 1} has no name")
        parent = section.attributes.get("parent")
        path = "." if parent is None else name if parent == "." else f"{parent}/{name}"
        if path in nodes:
            raise GodotSceneEditError(f"scene contains duplicate node path {path!r}")
        nodes[path] = section
    if "." not in nodes:
        raise GodotSceneEditError("scene has no root node")
    return nodes


def _add_node(lines: list[str], operation: dict[str, Any]) -> tuple[list[str], str]:
    nodes = _node_sections(lines)
    name = _required_text(operation, "name")
    node_type = _required_text(operation, "type")
    parent = _node_path(operation.get("parent", "."))
    if not _VALID_NODE_NAME_RE.fullmatch(name):
        raise GodotSceneEditError("node name cannot contain '/', brackets, or newlines")
    if parent not in nodes:
        raise GodotSceneEditError(f"parent node {parent!r} does not exist")
    path = name if parent == "." else f"{parent}/{name}"
    if path in nodes:
        raise GodotSceneEditError(f"node {path!r} already exists")

    properties = operation.get("properties") or {}
    if not isinstance(properties, dict):
        raise GodotSceneEditError("add_node properties must be an object")
    block = [
        f'[node name="{_escape(name)}" type="{_escape(node_type)}" parent="{_escape(parent)}"]'
    ]
    for prop_name, value in properties.items():
        prop = _property_name(prop_name)
        block.append(f"{prop} = {_raw_value(value)}")
    block.append("")

    sections = _sections(lines)
    connection = next((section for section in sections if section.kind == "connection"), None)
    insertion = connection.start if connection is not None else len(lines)
    while insertion > 0 and not lines[insertion - 1].strip():
        insertion -= 1
    if insertion and lines[insertion - 1].strip():
        block.insert(0, "")
    return lines[:insertion] + block + lines[insertion:], f"added node {path} ({node_type})"


def _remove_node(lines: list[str], operation: dict[str, Any]) -> tuple[list[str], str]:
    nodes = _node_sections(lines)
    path = _node_path(operation.get("node_path"))
    if path == ".":
        raise GodotSceneEditError("the scene root cannot be removed")
    if path not in nodes:
        raise GodotSceneEditError(f"node {path!r} does not exist")
    descendants = [candidate for candidate in nodes if candidate.startswith(path + "/")]
    recursive = operation.get("recursive") is True
    if descendants and not recursive:
        raise GodotSceneEditError(
            f"node {path!r} has children; set recursive=true to remove the subtree"
        )
    removed_paths = {path, *descendants}
    ranges = sorted(
        ((nodes[item].start, nodes[item].end) for item in removed_paths),
        reverse=True,
    )
    edited = list(lines)
    for start, end in ranges:
        del edited[start:end]
    return edited, f"removed node {path}" + (" recursively" if descendants else "")


def _set_property(lines: list[str], operation: dict[str, Any]) -> tuple[list[str], str]:
    nodes = _node_sections(lines)
    path = _node_path(operation.get("node_path"))
    section = nodes.get(path)
    if section is None:
        raise GodotSceneEditError(f"node {path!r} does not exist")
    name = _property_name(operation.get("property"))
    value = _raw_value(operation.get("value"))
    matches = _property_lines(lines, section, name)
    if len(matches) > 1:
        raise GodotSceneEditError(f"node {path!r} has duplicate property {name!r}")
    edited = list(lines)
    rendered = f"{name} = {value}"
    if matches:
        edited[matches[0]] = rendered
    else:
        insertion = section.end
        while insertion > section.start + 1 and not edited[insertion - 1].strip():
            insertion -= 1
        edited.insert(insertion, rendered)
    return edited, f"set {path}:{name}"


def _remove_property(lines: list[str], operation: dict[str, Any]) -> tuple[list[str], str]:
    nodes = _node_sections(lines)
    path = _node_path(operation.get("node_path"))
    section = nodes.get(path)
    if section is None:
        raise GodotSceneEditError(f"node {path!r} does not exist")
    name = _property_name(operation.get("property"))
    matches = _property_lines(lines, section, name)
    if not matches:
        raise GodotSceneEditError(f"node {path!r} has no property {name!r}")
    if len(matches) > 1:
        raise GodotSceneEditError(f"node {path!r} has duplicate property {name!r}")
    edited = list(lines)
    del edited[matches[0]]
    return edited, f"removed {path}:{name}"


def _property_lines(lines: list[str], section: SceneSection, name: str) -> list[int]:
    matches: list[int] = []
    for index in range(section.start + 1, section.end):
        match = _PROPERTY_RE.match(lines[index])
        if match and match.group(1) == name:
            matches.append(index)
    return matches


def _node_path(value: Any) -> str:
    path = str(value or "").strip().strip("/")
    if path in {"", "."}:
        return "."
    if path.startswith("./"):
        path = path[2:]
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise GodotSceneEditError(f"invalid root-relative node path {value!r}")
    return path


def _required_text(operation: dict[str, Any], key: str) -> str:
    value = str(operation.get(key) or "").strip()
    if not value:
        raise GodotSceneEditError(f"{key} is required")
    if any(character in value for character in "\r\n"):
        raise GodotSceneEditError(f"{key} cannot contain newlines")
    return value


def _property_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _VALID_PROPERTY_RE.fullmatch(name):
        raise GodotSceneEditError(f"invalid property name {name!r}")
    return name


def _raw_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GodotSceneEditError("property value must be a non-empty Godot expression string")
    rendered = value.strip()
    if "\n" in rendered or "\r" in rendered:
        raise GodotSceneEditError("property value must fit on one line")
    return rendered


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


__all__ = [
    "GodotSceneEditError",
    "GodotSceneEditResult",
    "edit_godot_scene",
    "scene_node_paths",
]
