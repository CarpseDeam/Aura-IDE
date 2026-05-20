from pathlib import Path

# 1. aura/repo_map.py
p = Path("aura/repo_map.py")
content = p.read_text("utf-8")
content = content.replace("import os\nfrom pathlib import Path", "import os\nimport time\nfrom pathlib import Path")
content = content.replace(
    "_repo_map_cache: dict[str, tuple[float, str]] = {}",
    """DEBOUNCE_SECONDS: float = 10.0

# Cache: workspace_root_str -> {"max_mtime": float, "cached_text": str, "last_checked": float}
_repo_map_cache: dict[str, dict[str, Any]] = {}""",
)

# Replace generate_repo_map body
old_gen = """    root_str = str(workspace_root.resolve())

    # Check cache
    current_mtime = _get_max_mtime(workspace_root)
    cached_mtime, cached_text = _repo_map_cache.get(root_str, (0.0, ""))
    if current_mtime == cached_mtime and cached_text:
        return cached_text"""

new_gen = """    root_str = str(workspace_root.resolve())
    now = time.time()
    entry = _repo_map_cache.get(root_str)
    if entry is not None:
        if now - entry.get("last_checked", 0.0) < DEBOUNCE_SECONDS:
            return entry["cached_text"]
        current_mtime = _get_max_mtime(workspace_root)
        if current_mtime == entry.get("max_mtime"):
            entry["last_checked"] = now
            return entry["cached_text"]
    else:
        current_mtime = _get_max_mtime(workspace_root)"""
content = content.replace(old_gen, new_gen)

content = content.replace(
    "_repo_map_cache[root_str] = (current_mtime, result)",
    '_repo_map_cache[root_str] = {"max_mtime": current_mtime, "cached_text": result, "last_checked": time.time()}',
)

# Add invalidate at the end
content += """

def invalidate_repo_map_cache(workspace_root: Path) -> None:
    \"\"\"Explicitly clear the cached repo map for a workspace root.\"\"\"
    root_str = str(workspace_root.resolve())
    _repo_map_cache.pop(root_str, None)
"""
p.write_text(content, "utf-8")


# 2. aura/conversation/tools/_write_mixin.py
p = Path("aura/conversation/tools/_write_mixin.py")
content = p.read_text("utf-8")
old_w = """        target.write_text(req.new_content, encoding="utf-8")
        rel_backup = ("""
new_w = """        target.write_text(req.new_content, encoding="utf-8")
        try:
            from aura.repo_map import invalidate_repo_map_cache
            invalidate_repo_map_cache(self._root)
        except Exception:
            pass
        rel_backup = ("""
content = content.replace(old_w, new_w)
p.write_text(content, "utf-8")


# 3. aura/conversation/manager.py
p = Path("aura/conversation/manager.py")
content = p.read_text("utf-8")
content = content.replace(
    'read_only_tools = {"read_file", "read_file_outline", "list_directory", "grep_search", "glob"}',
    'read_only_tools = {"read_file", "read_files", "read_file_outline", "list_directory", "grep_search", "glob"}',
)
p.write_text(content, "utf-8")


# 4. aura/craft/types.py
p = Path("aura/craft/types.py")
content = p.read_text("utf-8")
content = content.replace(
    "expected_dataclass_fields: list[str] = field(default_factory=list)",
    "expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)",
)
content = content.replace(
    '"expected_dataclass_fields": list(self.expected_dataclass_fields),',
    '"expected_dataclass_fields": {k: list(v) for k, v in self.expected_dataclass_fields.items()},',
)
old_fd = 'expected_dataclass_fields=list(data.get("expected_dataclass_fields", [])),'
new_fd = """expected_dataclass_fields=(
                lambda raw_fields: {
                    k: [str(item) for item in v] if isinstance(v, list) else []
                    for k, v in raw_fields.items()
                } if isinstance(raw_fields, dict) else {
                    str(item): [] for item in raw_fields
                } if isinstance(raw_fields, list) else {}
            )(data.get("expected_dataclass_fields")),"""
content = content.replace(old_fd, new_fd)
p.write_text(content, "utf-8")


# 5. aura/conversation/dispatch.py
p = Path("aura/conversation/dispatch.py")
content = p.read_text("utf-8")
content = content.replace(
    "expected_dataclass_fields: list[str] = field(default_factory=list)",
    "expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)",
)
content = content.replace(
    '"expected_dataclass_fields": list(self.expected_dataclass_fields),',
    '"expected_dataclass_fields": {k: list(v) for k, v in self.expected_dataclass_fields.items()},',
)
old_fd_disp = 'expected_dataclass_fields=_string_list(data.get("expected_dataclass_fields")),'
new_fd_disp = """expected_dataclass_fields=(
                lambda raw_fields: {
                    k: [str(item) for item in v] if isinstance(v, list) else []
                    for k, v in raw_fields.items()
                } if isinstance(raw_fields, dict) else {
                    str(item): [] for item in raw_fields
                } if isinstance(raw_fields, list) else {}
            )(data.get("expected_dataclass_fields")),"""
content = content.replace(old_fd_disp, new_fd_disp)

content = content.replace(
    "expected_dataclass_fields=list(req.expected_dataclass_fields)",
    "expected_dataclass_fields=dict(req.expected_dataclass_fields)",
)

old_dict = """            "non_goals": list(self.non_goals),
        }"""
new_dict = """            "non_goals": list(self.non_goals),
            "contract": self.contract.to_dict() if self.contract else None,
        }"""
content = content.replace(old_dict, new_dict)
p.write_text(content, "utf-8")


# 6. aura/conversation/tools/_schemas.py
p = Path("aura/conversation/tools/_schemas.py")
content = p.read_text("utf-8")
old_sch = """"expected_dataclass_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Specific field names that must exist on dataclass definitions, "
                        "e.g. ['id', 'name', 'created_at']. The ContractGate will verify these fields are present."
                    ),
                },"""
new_sch = """"expected_dataclass_fields": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "description": (
                        "A map from class names to lists of field names that must exist on dataclass definitions, "
                        "e.g. {'MyConfig': ['id', 'name']}. The ContractGate will verify these fields are present."
                    ),
                },"""
content = content.replace(old_sch, new_sch)
p.write_text(content, "utf-8")


# 7. aura/mcp_client.py
p = Path("aura/mcp_client.py")
content = p.read_text("utf-8")
content = content.replace(
    "def __init__(self, server_command: list[str]) -> None:",
    "def __init__(self, server_command: list[str], timeout: int = 30) -> None:\n        self._timeout = timeout",
)
content = content.replace("future.result(timeout=30)", "future.result(timeout=self._timeout)")
p.write_text(content, "utf-8")


# 8. aura/bridge/dispatch.py
p = Path("aura/bridge/dispatch.py")
content = p.read_text("utf-8")
content = content.replace(
    '__all__ = [\n    "_DispatchProxy",',
    'DISPATCH_DECISION_TIMEOUT: float = 300.0\n\n__all__ = [\n    "_DispatchProxy",',
)
old_wait = "pending.decision_event.wait()"
new_wait = """if not pending.decision_event.wait(timeout=DISPATCH_DECISION_TIMEOUT):
            pending.cancelled = True
            with self._lock:
                self._pending.pop(tool_call_id, None)
            return WorkerDispatchResult(
                ok=False,
                summary="dispatch decision timed out after 5 minutes",
                cancelled=True,
            )"""
content = content.replace(old_wait, new_wait)
p.write_text(content, "utf-8")

print("Done with replacements!")
