"""Code-intelligence adapters for Godot engine files.

Phase 2: project.godot parsing, rich .tscn/.tres scene-resource parsing,
enhanced GDScript symbols/references, and best-effort autoload awareness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from aura.code_intel.adapter import CodeIntelAdapter, register_adapter
from aura.code_intel.generic_adapter import GenericSymbolAdapter
from aura.code_intel.models import ReferenceEdge, SymbolInfo

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GodotProjectInfo:
    main_scene: str | None = None
    autoloads: dict[str, str] = field(default_factory=dict)
    plugins: list[str] = field(default_factory=list)
    input_actions: list[str] = field(default_factory=list)


@dataclass
class ExtResourceInfo:
    id: str
    type: str
    path: str
    line: int


@dataclass
class NodeInfo:
    name: str
    type: str | None = None
    parent: str | None = None
    instance_id: str | None = None
    line: int = 0


@dataclass
class GodotSceneInfo:
    ext_resources: dict[str, ExtResourceInfo] = field(default_factory=dict)
    nodes: list[NodeInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

# -- project.godot patterns --
_RE_RUN_MAIN_SCENE = re.compile(r'^run/main_scene\s*=\s*"(res://[^"]+)"')
_RE_AUTOLOAD = re.compile(r'^(\w+)\s*=\s*"\*?(res://[^"]+)"')
_RE_EDITOR_PLUGINS_ENABLED = re.compile(
    r'enabled\s*=\s*PackedStringArray\(([^)]*)\)'
)
_RE_INPUT_ACTION = re.compile(r'^([a-z_]\w+)\s*=\s*{')

# -- .tscn / .tres patterns --
_RE_EXT_RESOURCE = re.compile(
    r'\[ext_resource\s+[^\]]*?id="?([^"\s]+)"?\s+type="([^"]+)"\s+path="([^"]+)"'
)
_RE_NODE = re.compile(
    r'\[node\s+name="([^"]+)"(?:\s+type="([^"]*)")?'
    r'(?:\s+parent="([^"]*)")?(?:\s+instance=ExtResource\("?([^")]+)"?\))?'
)
_RE_SCRIPT_ASSIGN = re.compile(r'^\s*script\s*=\s*ExtResource\("?([^")]+)"?\)')
_RE_INSTANCE_ASSIGN = re.compile(r'instance\s*=\s*ExtResource\("?([^")]+)"?\)')
_RE_NODE_PATH_STR = re.compile(r'NodePath\("([^"]+)"\)')
_RE_CONNECTION = re.compile(
    r'\[connection\s+signal="([^"]+)"\s+from="([^"]+)"'
    r'\s+to="([^"]+)"\s+method="([^"]+)"\]'
)
_RE_RES_PATH = re.compile(r'path="(res://[^"]+)"')

# -- GDScript patterns (phase 1, kept for backward compat) --
_RE_EXTENDS_RES = re.compile(r'extends\s+["\'](res://[^"\']+)["\']')
_RE_PRELOAD_RES = re.compile(r'preload\s*\(\s*["\'](res://[^"\']+)["\']\s*\)')
_RE_LOAD_RES = re.compile(r'\bload\s*\(\s*["\'](res://[^"\']+)["\']\s*\)')

# -- GDScript patterns (phase 2) --
_RE_CLASS_NAME = re.compile(r'^\s*class_name\s+(\w+)')
_RE_SIGNAL_DEF = re.compile(r'^\s*signal\s+(\w+)\s*\(')
_RE_EXPORT_VAR = re.compile(r'^@export\s+var\s+(\w+)')
_RE_CONST = re.compile(r'^\s*const\s+(\w+)\s*=')
_RE_VAR_TOP = re.compile(r'^\s*(?:@\w+\s+)?var\s+(\w+)')
_RE_QUOTED_RES = re.compile(r'["\'](res://[^"\']+)["\']')
_RE_DOLLAR_NODE = re.compile(r'\$(\w+(?:/\w+)*)')
_RE_UNIQUE_NAME = re.compile(r'%(\w+)')
_RE_GET_NODE_CALL = re.compile(r'get_node\("([^"]+)"\)')
_RE_NODE_PATH_CALL = re.compile(r'NodePath\("([^"]+)"\)')
_RE_SIGNAL_EMIT = re.compile(r'(\w+)\.emit\s*\(')
_RE_CONNECT_CALL = re.compile(r'\.connect\s*\("([^"]+)"')
_RE_CONNECT_HANDLER = re.compile(r'connect\("[^"]+"\s*,\s*(\w+)')
_RE_AUTOLOAD_USAGE = re.compile(
    r'(?<![.\w])([A-Z][a-zA-Z0-9_]+)\.([a-z_]\w*)\s*\('
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _res_to_workspace_path(uri: str) -> str:
    """Convert 'res://foo/bar.gd' -> 'foo/bar.gd'. Non-res:// returned as-is."""
    return uri[6:] if uri.startswith("res://") else uri


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    """Deduplicate preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _basename_symbol(path: str) -> str:
    """Return the basename of a path, e.g. 'foo/bar.gd' -> 'bar.gd'."""
    return Path(path).name


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_project_godot(content: str) -> GodotProjectInfo:
    """Parse a project.godot file and return structured info."""
    info = GodotProjectInfo()
    section: str | None = None

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue

        sm = re.match(r'^\[(\w+)\]$', stripped)
        if sm:
            section = sm.group(1)
            continue
        if section is None:
            continue

        if section == "application":
            m = _RE_RUN_MAIN_SCENE.search(stripped)
            if m:
                info.main_scene = _res_to_workspace_path(m.group(1))

        elif section == "autoload":
            m = _RE_AUTOLOAD.search(stripped)
            if m:
                name = m.group(1)
                path = m.group(2)
                info.autoloads[name] = _res_to_workspace_path(path)

        elif section == "editor_plugins":
            m = _RE_EDITOR_PLUGINS_ENABLED.search(stripped)
            if m:
                for pm in re.finditer(r'"([^"]+)"', m.group(1)):
                    val = pm.group(1)
                    if val.startswith("res://"):
                        info.plugins.append(_res_to_workspace_path(val))

        elif section == "input":
            m = _RE_INPUT_ACTION.search(stripped)
            if m:
                info.input_actions.append(m.group(1))

    return info


def parse_godot_scene_resource(content: str) -> GodotSceneInfo:
    """Parse a .tscn or .tres file and return structured info."""
    info = GodotSceneInfo()

    for i, line in enumerate(content.splitlines(), start=1):
        m = _RE_EXT_RESOURCE.search(line)
        if m:
            ext_id = m.group(1)
            info.ext_resources[ext_id] = ExtResourceInfo(
                id=ext_id,
                type=m.group(2),
                path=_res_to_workspace_path(m.group(3)),
                line=i,
            )
            continue

        m = _RE_NODE.search(line)
        if m:
            info.nodes.append(
                NodeInfo(
                    name=m.group(1),
                    type=m.group(2) or None,
                    parent=m.group(3) or None,
                    instance_id=m.group(4) or None,
                    line=i,
                )
            )

    return info


# ---------------------------------------------------------------------------
# GDScript scanning helpers
# ---------------------------------------------------------------------------


def _scan_gdscript_resource_paths(
    content: str,
) -> list[tuple[int, str, str, str]]:
    """Scan GDScript content for res:// references.

    Returns list of (line_number, kind, ws_path, symbol_name).
    """
    results: list[tuple[int, str, str, str]] = []
    seen: set[tuple[int, str]] = set()

    for line_no, line in enumerate(content.splitlines(), start=1):
        m = _RE_EXTENDS_RES.search(line)
        if m:
            ws = _res_to_workspace_path(m.group(1))
            if (line_no, ws) not in seen:
                seen.add((line_no, ws))
                results.append(
                    (line_no, "inherit", ws, _basename_symbol(ws))
                )

        for pattern in (_RE_PRELOAD_RES, _RE_LOAD_RES):
            for m in pattern.finditer(line):
                ws = _res_to_workspace_path(m.group(1))
                if (line_no, ws) not in seen:
                    seen.add((line_no, ws))
                    results.append(
                        (line_no, "reference", ws, _basename_symbol(ws))
                    )

        for m in _RE_QUOTED_RES.finditer(line):
            ws = _res_to_workspace_path(m.group(1))
            if (line_no, ws) not in seen:
                seen.add((line_no, ws))
                results.append(
                    (line_no, "reference", ws, _basename_symbol(ws))
                )

    return results


def _scan_gdscript_symbols(
    content: str, file_path: str
) -> list[SymbolInfo]:
    """Scan GDScript for Godot-specific symbols not captured by tree-sitter.

    Returns class_name declarations, signal definitions, @export vars,
    consts, and top-level vars.
    """
    syms: list[SymbolInfo] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        m = _RE_CLASS_NAME.search(line)
        if m:
            syms.append(
                SymbolInfo(
                    name=m.group(1),
                    kind="class",
                    file=file_path,
                    line=line_no,
                )
            )
            continue
        m = _RE_SIGNAL_DEF.search(line)
        if m:
            syms.append(
                SymbolInfo(
                    name=m.group(1),
                    kind="function",
                    file=file_path,
                    line=line_no,
                )
            )
            continue
        m = _RE_EXPORT_VAR.search(line)
        if m:
            syms.append(
                SymbolInfo(
                    name=m.group(1),
                    kind="variable",
                    file=file_path,
                    line=line_no,
                )
            )
            continue
        m = _RE_CONST.search(line)
        if m:
            syms.append(
                SymbolInfo(
                    name=m.group(1),
                    kind="variable",
                    file=file_path,
                    line=line_no,
                )
            )
            continue
        m = _RE_VAR_TOP.search(line)
        if m:
            syms.append(
                SymbolInfo(
                    name=m.group(1),
                    kind="variable",
                    file=file_path,
                    line=line_no,
                )
            )
            continue
    return syms


def _scan_gdscript_references(
    content: str, file_path: str
) -> list[ReferenceEdge]:
    """Scan GDScript for all reference edges except autoload usage."""
    refs: list[ReferenceEdge] = []

    # 1. File resource references (extends, preload, load, quoted)
    for line_no, kind, ws_path, sym_name in _scan_gdscript_resource_paths(
        content
    ):
        refs.append(
            ReferenceEdge(
                source_file=file_path,
                source_symbol=None,
                target_file=ws_path,
                target_symbol=sym_name,
                line=line_no,
                kind=kind,
            )
        )

    # 2-5. Node path, unique name, get_node, NodePath references
    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in _RE_DOLLAR_NODE.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"node:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )
        for m in _RE_UNIQUE_NAME.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"node:%{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )
        for m in _RE_GET_NODE_CALL.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"node:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )
        for m in _RE_NODE_PATH_CALL.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"node:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )

    # 6. Signal emits
    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in _RE_SIGNAL_EMIT.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"signal:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )

    # 7. Signal connects
    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in _RE_CONNECT_CALL.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"signal:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )
        for m in _RE_CONNECT_HANDLER.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"method:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )

    return refs


def _scan_gdscript_dependencies(content: str) -> list[str]:
    """Collect unique ws_paths from resource references, first-seen order."""
    seen: set[str] = set()
    deps: list[str] = []
    for _, _, ws_path, _ in _scan_gdscript_resource_paths(content):
        if ws_path not in seen:
            seen.add(ws_path)
            deps.append(ws_path)
    return deps


def _scan_autoload_usage(
    content: str, file_path: str
) -> list[ReferenceEdge]:
    """Detect capitalized singleton-style access as autoload candidates.

    Autoload resolution to script paths requires project.godot context —
    unresolved for now.
    """
    refs: list[ReferenceEdge] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in _RE_AUTOLOAD_USAGE.finditer(line):
            refs.append(
                ReferenceEdge(
                    source_file=file_path,
                    source_symbol=None,
                    target_file=None,
                    target_symbol=f"autoload:{m.group(1)}",
                    line=line_no,
                    kind="usage",
                )
            )
    return refs


# ---------------------------------------------------------------------------
# Adapter: project.godot
# ---------------------------------------------------------------------------


class GodotProjectAdapter(CodeIntelAdapter):
    """Adapter for project.godot files."""

    @property
    def language_id(self) -> str:
        return "godot_project"

    def detect(self, file_path: str, content: str | None = None) -> bool:
        return Path(file_path).name == "project.godot"

    def parse(
        self, file_path: str, content: str
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return (self.symbols(file_path, content), self.references(file_path, content), [])

    def outline(self, file_path: str, content: str) -> dict[str, Any]:
        info = parse_project_godot(content)
        deps = self.dependencies(file_path, content)
        return {
            "language": "godot_project",
            "imports": deps,
            "classes": [],
            "functions": [],
        }

    def symbols(self, file_path: str, content: str) -> list[Any]:
        info = parse_project_godot(content)
        syms: list[SymbolInfo] = []
        for name in info.autoloads:
            syms.append(
                SymbolInfo(
                    name=name,
                    kind="variable",
                    file=file_path,
                    line=0,
                )
            )
        return syms

    def references(self, file_path: str, content: str) -> list[Any]:
        return []

    def dependencies(self, file_path: str, content: str) -> list[str]:
        info = parse_project_godot(content)
        deps: list[str] = []
        if info.main_scene:
            deps.append(info.main_scene)
        deps.extend(info.autoloads.values())
        deps.extend(info.plugins)
        return _dedupe_preserve_order(deps)


# ---------------------------------------------------------------------------
# Adapter: .tscn / .tres
# ---------------------------------------------------------------------------


class GodotSceneResourceAdapter(CodeIntelAdapter):
    """Adapter for Godot scene/resource files (.tscn, .tres)."""

    @property
    def language_id(self) -> str:
        return "godot_resource"

    def detect(self, file_path: str, content: str | None = None) -> bool:
        return file_path.endswith(".tscn") or file_path.endswith(".tres")

    def parse(
        self, file_path: str, content: str
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return (self.symbols(file_path, content), self.references(file_path, content), [])

    def outline(self, file_path: str, content: str) -> dict[str, Any]:
        info = parse_godot_scene_resource(content)
        deps = self.dependencies(file_path, content)
        result: dict[str, Any] = {
            "language": "godot_scene" if file_path.endswith(".tscn") else "godot_resource",
            "imports": deps,
            "classes": [],
            "functions": [],
        }
        if file_path.endswith(".tscn"):
            result["nodes"] = [
                {"name": n.name, "type": n.type, "parent": n.parent, "line": n.line}
                for n in info.nodes
            ]
        return result

    def symbols(self, file_path: str, content: str) -> list[Any]:
        info = parse_godot_scene_resource(content)
        syms: list[SymbolInfo] = []
        if file_path.endswith(".tscn"):
            for node in info.nodes:
                syms.append(
                    SymbolInfo(
                        name=node.name,
                        kind="variable",
                        file=file_path,
                        line=node.line,
                        parent=node.parent,
                    )
                )
        return syms

    def references(self, file_path: str, content: str) -> list[Any]:
        info = parse_godot_scene_resource(content)
        refs: list[ReferenceEdge] = []
        seen_paths: set[str] = set()

        # 1. ExtResource references (script assignments)
        for i, line in enumerate(content.splitlines(), start=1):
            m = _RE_SCRIPT_ASSIGN.search(line)
            if m:
                ext_id = m.group(1)
                ext = info.ext_resources.get(ext_id)
                if ext and ext.path not in seen_paths:
                    seen_paths.add(ext.path)
                    refs.append(
                        ReferenceEdge(
                            source_file=file_path,
                            source_symbol=None,
                            target_file=ext.path,
                            target_symbol=_basename_symbol(ext.path),
                            line=i,
                            kind="reference",
                        )
                    )

        # 2. Scene instance references
        for i, line in enumerate(content.splitlines(), start=1):
            m = _RE_INSTANCE_ASSIGN.search(line)
            if m:
                ext_id = m.group(1)
                ext = info.ext_resources.get(ext_id)
                if ext and ext.path not in seen_paths:
                    seen_paths.add(ext.path)
                    refs.append(
                        ReferenceEdge(
                            source_file=file_path,
                            source_symbol=None,
                            target_file=ext.path,
                            target_symbol=_basename_symbol(ext.path),
                            line=i,
                            kind="reference",
                        )
                    )

        # 3. NodePath usages
        for i, line in enumerate(content.splitlines(), start=1):
            for m in _RE_NODE_PATH_STR.finditer(line):
                refs.append(
                    ReferenceEdge(
                        source_file=file_path,
                        source_symbol=None,
                        target_file=None,
                        target_symbol=f"node:{m.group(1)}",
                        line=i,
                        kind="usage",
                    )
                )

        # 4. Signal connection usages
        for i, line in enumerate(content.splitlines(), start=1):
            m = _RE_CONNECTION.search(line)
            if m:
                refs.append(
                    ReferenceEdge(
                        source_file=file_path,
                        source_symbol=None,
                        target_file=None,
                        target_symbol=f"signal:{m.group(1)}",
                        line=i,
                        kind="usage",
                    )
                )
                refs.append(
                    ReferenceEdge(
                        source_file=file_path,
                        source_symbol=None,
                        target_file=None,
                        target_symbol=f"method:{m.group(4)}",
                        line=i,
                        kind="usage",
                    )
                )

        # 5. Remaining path="res://..." references not already captured
        existing_targets = {(r.target_file, r.line) for r in refs if r.target_file}
        for i, line in enumerate(content.splitlines(), start=1):
            for m in _RE_RES_PATH.finditer(line):
                ws = _res_to_workspace_path(m.group(1))
                if (ws, i) not in existing_targets:
                    existing_targets.add((ws, i))
                    refs.append(
                        ReferenceEdge(
                            source_file=file_path,
                            source_symbol=None,
                            target_file=ws,
                            target_symbol=_basename_symbol(ws),
                            line=i,
                            kind="reference",
                        )
                    )

        return refs

    def dependencies(self, file_path: str, content: str) -> list[str]:
        info = parse_godot_scene_resource(content)
        seen: set[str] = set()
        deps: list[str] = []

        for ext in info.ext_resources.values():
            if ext.path not in seen:
                seen.add(ext.path)
                deps.append(ext.path)

        for m in _RE_RES_PATH.finditer(content):
            ws = _res_to_workspace_path(m.group(1))
            if ws not in seen:
                seen.add(ws)
                deps.append(ws)

        return deps


# ---------------------------------------------------------------------------
# Adapter: .gd (GDScript)
# ---------------------------------------------------------------------------


class GodotGDScriptAdapter(CodeIntelAdapter):
    """Code-intelligence adapter for GDScript (.gd) files.

    Phase 2: tree-sitter symbols/outline + custom Godot symbols + rich references.
    """

    @property
    def language_id(self) -> str:
        return "gdscript"

    def __init__(self) -> None:
        self._generic = GenericSymbolAdapter("gdscript", (".gd",))

    def detect(self, file_path: str, content: str | None = None) -> bool:
        return file_path.endswith(".gd")

    def parse(
        self, file_path: str, content: str
    ) -> tuple[list[Any], list[Any], list[Any]]:
        ts_syms, _, diags = self._generic.parse(file_path, content)
        godot_syms = _scan_gdscript_symbols(content, file_path)

        seen: set[tuple[str, str, int]] = set()
        combined: list[SymbolInfo] = []
        for sym in ts_syms + godot_syms:
            key = (sym.name, sym.kind, sym.line)
            if key not in seen:
                seen.add(key)
                combined.append(sym)

        refs = self.references(file_path, content)
        return (combined, refs, diags)

    def outline(self, file_path: str, content: str) -> dict[str, Any]:
        return self._generic.outline(file_path, content)

    def symbols(self, file_path: str, content: str) -> list[Any]:
        syms, _, _ = self.parse(file_path, content)
        return syms

    def references(self, file_path: str, content: str) -> list[Any]:
        refs: list[ReferenceEdge] = []
        refs.extend(_scan_gdscript_references(content, file_path))
        refs.extend(_scan_autoload_usage(content, file_path))
        return refs

    def dependencies(self, file_path: str, content: str) -> list[str]:
        return _scan_gdscript_dependencies(content)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_adapter(GodotGDScriptAdapter())
register_adapter(GodotSceneResourceAdapter())
register_adapter(GodotProjectAdapter())
