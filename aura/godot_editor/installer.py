"""Install Aura's bundled EditorPlugin into a Godot project."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from aura.godot_editor.client import CONFIG_PATH

ADDON_SETTING = "res://addons/aura_bridge/plugin.cfg"
DEFAULT_PORT = 17891
_ENABLED_RE = re.compile(r"(?m)^enabled\s*=\s*PackedStringArray\((.*?)\)[ \t]*$")


@dataclass(frozen=True)
class GodotEditorBridgeInstallResult:
    files: tuple[str, ...]
    port: int
    plugin_enabled: bool


def install_editor_bridge(
    project_root: Path,
    port: int = DEFAULT_PORT,
    *,
    enable_plugin: bool = False,
) -> GodotEditorBridgeInstallResult:
    root = project_root.resolve()
    project_file = root / "project.godot"
    if not project_file.is_file():
        raise ValueError("workspace is not a Godot project (project.godot is missing)")
    if not 1024 <= int(port) <= 65535:
        raise ValueError("port must be between 1024 and 65535")

    source = Path(__file__).with_name("addon")
    target = root / "addons" / "aura_bridge"
    if not source.is_dir():
        raise RuntimeError("bundled Aura Godot editor addon is missing")
    target.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = source_file.relative_to(source)
        target_file = target / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target_file)
        installed.append(target_file.relative_to(root).as_posix())

    config_path = root / CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    token = _existing_token(config_path) or secrets.token_urlsafe(32)
    config_path.write_text(
        json.dumps({"protocol": 1, "host": "127.0.0.1", "port": int(port), "token": token}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    installed.append(CONFIG_PATH.as_posix())

    if enable_plugin:
        original = project_file.read_text(encoding="utf-8-sig")
        updated = enable_plugin_setting(original)
        if updated != original:
            project_file.write_text(updated, encoding="utf-8")
            installed.append("project.godot")
    return GodotEditorBridgeInstallResult(tuple(installed), int(port), enable_plugin)


def enable_plugin_setting(content: str) -> str:
    section_match = re.search(r"(?m)^\[editor_plugins\]\s*$", content)
    if section_match is None:
        suffix = "" if content.endswith("\n") else "\n"
        return content + suffix + f'\n[editor_plugins]\n\nenabled=PackedStringArray("{ADDON_SETTING}")\n'

    next_section = re.search(r"(?m)^\[[^\n]+\]\s*$", content[section_match.end() :])
    section_end = section_match.end() + (next_section.start() if next_section else len(content))
    section = content[section_match.end() : section_end]
    enabled = _ENABLED_RE.search(section)
    if enabled is None:
        insertion = f'\n\nenabled=PackedStringArray("{ADDON_SETTING}")'
        return content[:section_end].rstrip() + insertion + "\n\n" + content[section_end:].lstrip("\n")
    values = re.findall(r'"((?:\\.|[^"\\])*)"', enabled.group(1))
    if ADDON_SETTING in values:
        return content
    values.append(ADDON_SETTING)
    replacement = "enabled=PackedStringArray(" + ", ".join(json.dumps(value) for value in values) + ")"
    absolute_start = section_match.end() + enabled.start()
    absolute_end = section_match.end() + enabled.end()
    return content[:absolute_start] + replacement + content[absolute_end:]


def _existing_token(config_path: Path) -> str:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    token = str(raw.get("token") or "")
    return token if len(token) >= 24 else ""


__all__ = ["GodotEditorBridgeInstallResult", "enable_plugin_setting", "install_editor_bridge"]
