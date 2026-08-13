"""Tests for the read-only Reference Folder capability.

Covers ReferenceRootAccess trust/path resolution, the read_reference_file
tool handler, catalog exposure, effect classification, and — critically —
that none of this weakens the ordinary workspace jail.
"""

from __future__ import annotations

from pathlib import Path

from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect
from aura.conversation.tools.reference_root import ReferenceRootAccess
from aura.conversation.tools.registry import ToolRegistry

# ── A. ReferenceRootAccess ───────────────────────────────────────────────────


def test_no_root_is_unavailable(tmp_path: Path) -> None:
    access = ReferenceRootAccess(tmp_path)
    assert access.is_available is False
    assert access.root is None
    assert access.name is None


def test_valid_separate_directory_can_attach(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    reference = tmp_path / "Foo-v1"
    workspace.mkdir()
    reference.mkdir()

    access = ReferenceRootAccess(workspace)
    ok, message = access.attach(reference)

    assert ok is True, message
    assert access.is_available is True
    assert access.root == reference.resolve()
    assert access.name == "Foo-v1"


def test_root_must_exist_and_be_a_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    missing = tmp_path / "does-not-exist"
    a_file = tmp_path / "some_file.txt"
    a_file.write_text("x", encoding="utf-8")

    access = ReferenceRootAccess(workspace)

    ok, _ = access.attach(missing)
    assert ok is False
    assert access.is_available is False

    ok, _ = access.attach(a_file)
    assert ok is False
    assert access.is_available is False


def test_reference_candidate_must_be_absolute(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    access = ReferenceRootAccess(workspace)

    ok, message = access.attach(Path("relative-reference"))

    assert ok is False
    assert "absolute" in message.lower()


def test_workspace_itself_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()

    access = ReferenceRootAccess(workspace)
    ok, message = access.attach(workspace)

    assert ok is False
    assert "workspace" in message.lower()
    assert access.is_available is False


def test_reference_beneath_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    nested = workspace / "vendor" / "old-lib"
    nested.mkdir(parents=True)

    access = ReferenceRootAccess(workspace)
    ok, _ = access.attach(nested)

    assert ok is False
    assert access.is_available is False


def test_reference_containing_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "Projects" / "Foo-v2"
    workspace.mkdir(parents=True)
    outer = tmp_path / "Projects"

    access = ReferenceRootAccess(workspace)
    ok, _ = access.attach(outer)

    assert ok is False
    assert access.is_available is False


def _attached(tmp_path: Path) -> ReferenceRootAccess:
    workspace = tmp_path / "Foo-v2"
    reference = tmp_path / "Foo-v1"
    workspace.mkdir()
    reference.mkdir()
    access = ReferenceRootAccess(workspace)
    ok, message = access.attach(reference)
    assert ok is True, message
    return access


def test_resolve_rejects_dotdot(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    try:
        access.resolve("../Foo-v2/secret.txt")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_resolve_rejects_absolute_path(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    try:
        access.resolve(str(tmp_path / "Foo-v2" / "secret.txt"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_resolve_rejects_rooted_without_drive_on_any_platform(tmp_path: Path) -> None:
    """``/etc/passwd``-shaped input must be rejected even where pathlib does
    not classify it as absolute (Windows, no drive letter)."""
    access = _attached(tmp_path)
    try:
        access.resolve("/etc/passwd")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_resolve_rejects_empty_path(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    try:
        access.resolve("")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_resolve_requires_attached_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    access = ReferenceRootAccess(workspace)
    try:
        access.resolve("src/auth.py")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "attached" in str(exc).lower()


def test_resolve_accepts_ordinary_relative_path(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    reference_root = access.root
    (reference_root / "src").mkdir()
    (reference_root / "src" / "auth.py").write_text("x", encoding="utf-8")

    resolved = access.resolve("src/auth.py")
    assert resolved == (reference_root / "src" / "auth.py").resolve()


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the reference root pointing outside it must not
    grant access to what it points at."""
    access = _attached(tmp_path)
    reference_root = access.root
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")

    link = reference_root / "escape_link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink creation not permitted in this environment")

    try:
        access.resolve("escape_link.txt")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_clear_removes_authorization(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    assert access.is_available is True
    access.clear()
    assert access.is_available is False
    assert access.root is None


def test_set_workspace_root_clears_attached_reference(tmp_path: Path) -> None:
    access = _attached(tmp_path)
    assert access.is_available is True

    other_workspace = tmp_path / "Other-Project"
    other_workspace.mkdir()
    access.set_workspace_root(other_workspace)

    assert access.is_available is False


# ── B. Tool execution ────────────────────────────────────────────────────────


def _make_registry_with_reference(tmp_path: Path) -> ToolRegistry:
    workspace = tmp_path / "Foo-v2"
    reference = tmp_path / "Foo-v1"
    workspace.mkdir()
    reference.mkdir()
    (reference / "src").mkdir()
    (reference / "src" / "auth.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )

    registry = ToolRegistry(workspace_root=workspace)
    ok, message = registry.begin_reference_turn(reference)
    assert ok is True, message
    return registry


def test_attached_reference_file_can_be_read(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)

    result = registry.execute("read_reference_file", {"path": "src/auth.py"}, approval_cb=None)

    assert result.ok is True
    assert result.payload["ok"] is True
    assert "line 1" in result.payload["content"]
    assert result.payload["source"] == "reference"
    assert result.payload["reference_name"] == "Foo-v1"
    assert result.payload["read_only"] is True


def test_offset_limit_works_like_read_file(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)

    result = registry.execute(
        "read_reference_file",
        {"path": "src/auth.py", "offset": 3, "limit": 2},
        approval_cb=None,
    )

    assert result.ok is True
    content = result.payload["content"]
    assert "line 3" in content
    assert "line 4" in content
    assert "line 5" not in content
    assert "line 1" not in content


def test_payload_exposes_relative_path_not_absolute_reference_root(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)
    reference_root = registry._reference_root.root

    result = registry.execute("read_reference_file", {"path": "src/auth.py"}, approval_cb=None)

    assert result.ok is True
    returned_path = str(result.payload["path"])
    assert str(reference_root) not in returned_path
    assert "auth.py" in returned_path


def test_no_attached_reference_returns_deterministic_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    registry = ToolRegistry(workspace_root=workspace)

    result = registry.execute("read_reference_file", {"path": "src/auth.py"}, approval_cb=None)

    assert result.ok is False
    assert result.payload["failure_class"] == "reference_root_unavailable"
    assert "user-authorized" in result.payload["error"]


# ── C. Catalog exposure ──────────────────────────────────────────────────────


def _tool_names(tool_defs: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in tool_defs}


def test_without_attached_reference_production_catalog_is_unchanged(tmp_path: Path) -> None:
    from tests.test_production_tool_surface import EXPECTED_PRODUCTION_TOOLS

    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    registry = ToolRegistry(workspace_root=workspace)

    assert _tool_names(registry.tool_defs()) == EXPECTED_PRODUCTION_TOOLS
    assert "read_reference_file" not in _tool_names(registry.tool_defs())


def test_with_attached_reference_tool_appears(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)
    assert "read_reference_file" in _tool_names(registry.tool_defs())


def test_clearing_reference_removes_the_tool(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)
    assert "read_reference_file" in _tool_names(registry.tool_defs())

    registry.clear_reference_authorization()

    assert "read_reference_file" not in _tool_names(registry.tool_defs())


def test_appears_in_global_read_only_mode_when_attached(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    reference = tmp_path / "Foo-v1"
    workspace.mkdir()
    reference.mkdir()

    registry = ToolRegistry(workspace_root=workspace, read_only=True)
    ok, message = registry.begin_reference_turn(reference)
    assert ok is True, message

    assert "read_reference_file" in _tool_names(registry.tool_defs())


def test_catalog_build_tool_defs_reference_flag_directly() -> None:
    catalog = ToolCatalog()
    without = _tool_names(catalog.build_tool_defs(read_only=False))
    with_ref = _tool_names(
        catalog.build_tool_defs(read_only=False, reference_available=True)
    )
    assert "read_reference_file" not in without
    assert "read_reference_file" in with_ref


# ── D. Effect model ──────────────────────────────────────────────────────────


def test_read_reference_file_is_observation() -> None:
    assert BUILTIN_TOOL_EFFECTS["read_reference_file"] is ToolEffect.OBSERVATION


def test_registry_tool_effect_lookup_agrees(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)
    assert registry.tool_effect("read_reference_file") is ToolEffect.OBSERVATION


# ── E. Existing workspace jail is unweakened ─────────────────────────────────


def test_read_file_still_rejects_external_path(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)

    result = registry.execute(
        "read_file", {"path": str(tmp_path / "Foo-v1" / "src" / "auth.py")}, approval_cb=None
    )

    assert result.ok is False


def test_attaching_reference_does_not_make_it_resolvable_through_resolve_in_root(
    tmp_path: Path,
) -> None:
    registry = _make_registry_with_reference(tmp_path)
    try:
        registry._resolve_in_root(str(tmp_path / "Foo-v1" / "src" / "auth.py"))
        assert False, "expected ValueError: reference root must not be workspace-resolvable"
    except ValueError:
        pass


def test_reference_root_access_object_is_not_used_by_write_handlers(tmp_path: Path) -> None:
    """write_file must remain jailed to the workspace even with a reference
    attached — the ReferenceRootAccess object must never reach write/patch/
    delete/terminal/MCP/dynamic/Git handlers."""
    registry = _make_registry_with_reference(tmp_path)

    def approve(_request):
        from aura.conversation.tools._types import ApprovalDecision

        return ApprovalDecision(action="approve")

    result = registry.execute(
        "write_file",
        {"path": "../Foo-v1/src/auth.py", "content": "pwned"},
        approval_cb=approve,
    )
    assert result.ok is False


def test_workspace_switch_clears_reference_authorization(tmp_path: Path) -> None:
    registry = _make_registry_with_reference(tmp_path)
    assert registry.reference_root_available is True

    other_workspace = tmp_path / "Other-Project"
    other_workspace.mkdir()
    registry.set_workspace_root(other_workspace)

    assert registry.reference_root_available is False
    assert "read_reference_file" not in _tool_names(registry.tool_defs())


def test_broad_filesystem_and_user_roots_are_rejected(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for name in ("Desktop", "Documents", "Downloads", "OneDrive"):
        (home / name).mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    access = ReferenceRootAccess(workspace)
    candidates = [
        Path(home.anchor or str(home)),
        home,
        *(home / n for n in ("Desktop", "Documents", "Downloads", "OneDrive")),
    ]
    for candidate in candidates:
        ok, _message = access.attach(candidate)
        assert ok is False, candidate
        assert access.is_available is False


def test_reference_search_uses_a_dedicated_index_and_decorates_payload(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    (workspace / "workspace_only.py").write_text(
        "workspace_unique_marker = True\n", encoding="utf-8"
    )
    (reference / "reference_only.py").write_text(
        "reference_unique_marker = True\n", encoding="utf-8"
    )

    registry = ToolRegistry(workspace)
    ok, message = registry.begin_reference_turn(reference)
    assert ok is True, message

    result = registry.execute(
        "search_codebase",
        {"query": "reference_unique_marker", "source": "reference"},
        approval_cb=None,
    )

    assert result.ok is True
    assert result.payload["source"] == "reference"
    assert result.payload["reference_name"] == "reference"
    assert result.payload["read_only"] is True
    assert result.payload["results"][0]["path"] == "reference_only.py"
    assert str(reference) not in str(result.payload)
    assert "reference_only.py" not in registry._code_intel_index.file_paths()
    assert registry._reference_codebase_index is not None


def test_reference_search_without_authorization_is_unavailable(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    result = registry.execute(
        "search_codebase",
        {"query": "anything", "source": "reference"},
        approval_cb=None,
    )
    assert result.ok is False
    assert result.payload["failure_class"] == "reference_root_unavailable"


def test_search_source_validation_and_workspace_default(tmp_path: Path) -> None:
    marker = tmp_path / "workspace.py"
    marker.write_text("workspace_default_marker = True\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    implicit = registry.execute(
        "search_codebase", {"query": "workspace_default_marker"}, approval_cb=None
    )
    explicit = registry.execute(
        "search_codebase",
        {"query": "workspace_default_marker", "source": "workspace"},
        approval_cb=None,
    )
    invalid = registry.execute(
        "search_codebase", {"query": "x", "source": "other"}, approval_cb=None
    )

    assert implicit.payload == explicit.payload
    assert invalid.ok is False
    assert "workspace" in invalid.payload["error"]


def test_reference_index_is_released_with_turn_authorization(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    (reference / "old.py").write_text("old_marker = True\n", encoding="utf-8")
    registry = ToolRegistry(workspace)
    ok, message = registry.begin_reference_turn(reference)
    assert ok is True, message
    registry.execute(
        "search_codebase", {"query": "old_marker", "source": "reference"}, approval_cb=None
    )
    assert registry._reference_codebase_index is not None

    registry.clear_reference_authorization()

    assert registry._reference_codebase_index is None
    assert registry.reference_root_available is False
