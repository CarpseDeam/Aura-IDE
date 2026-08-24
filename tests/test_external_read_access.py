"""Tests for explicitly authorized read-only access outside the workspace.

Covers ExternalReadAccess (the single allowlist owner), the read and search
handlers that consult it, catalog exposure, and — critically — that none of
this weakens the ordinary workspace jail or reaches any mutation path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.external_read import ExternalReadAccess
from aura.conversation.tools.registry import ToolRegistry

# ── A. ExternalReadAccess ────────────────────────────────────────────────────


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    return workspace


def test_nothing_authorized_is_unavailable(tmp_path: Path) -> None:
    access = ExternalReadAccess(_workspace(tmp_path))
    assert access.is_available is False
    assert access.files == ()
    assert access.directories == ()
    assert access.display_names == ()


def test_a_directory_authorizes_its_whole_tree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    (external / "src").mkdir(parents=True)
    (external / "src" / "auth.py").write_text("x", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    assert access.authorize([external]) == (external.resolve(),)

    assert access.is_available is True
    assert access.display_names == ("Foo-v1",)
    assert access.resolve(str(external / "src" / "auth.py")) == (
        external / "src" / "auth.py"
    ).resolve()
    assert access.resolve(str(external)) == external.resolve()


def test_a_file_authorizes_only_itself(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = tmp_path / "Notes"
    folder.mkdir()
    named = folder / "design doc.md"
    named.write_text("named", encoding="utf-8")
    sibling = folder / "secrets.md"
    sibling.write_text("sibling", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    access.authorize([named])

    assert access.resolve(str(named)) == named.resolve()
    with pytest.raises(ValueError):
        access.resolve(str(sibling))
    with pytest.raises(ValueError):
        access.resolve(str(folder))


def test_a_directory_plus_a_file_inside_it_is_normalized_not_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    external.mkdir()
    inside = external / "README.md"
    inside.write_text("x", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    authorized = access.authorize([external, inside])

    # The tree already covers the file, so the file is not held twice.
    assert authorized == (external.resolve(),)
    assert access.files == ()
    assert access.resolve(str(inside)) == inside.resolve()


def test_nested_directories_are_normalized(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outer = tmp_path / "Projects"
    inner = outer / "Foo-v1"
    inner.mkdir(parents=True)

    access = ExternalReadAccess(workspace)
    assert access.authorize([outer, inner]) == (outer.resolve(),)
    assert access.resolve(str(inner / "anything.txt")) == (inner / "anything.txt").resolve()


def test_several_independent_locations_are_all_authorized(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = tmp_path / "Alpha"
    second = tmp_path / "Beta"
    first.mkdir()
    second.mkdir()
    loose = tmp_path / "loose.md"
    loose.write_text("x", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    authorized = access.authorize([first, second, loose])

    assert set(authorized) == {first.resolve(), second.resolve(), loose.resolve()}
    assert access.resolve(str(first / "a.txt")) == (first / "a.txt").resolve()
    assert access.resolve(str(second / "b.txt")) == (second / "b.txt").resolve()
    assert access.resolve(str(loose)) == loose.resolve()


def test_broad_user_locations_are_accepted_when_explicitly_named(
    tmp_path: Path, monkeypatch
) -> None:
    """Naming a location is the authorization. There is no folder denylist."""
    workspace = _workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    broad = [home]
    for name in ("Desktop", "Documents", "Downloads", "OneDrive"):
        folder = home / name
        folder.mkdir()
        broad.append(folder)
    broad.append(home / "Desktop" / "Work")
    (home / "Desktop" / "Work").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    for candidate in broad:
        access = ExternalReadAccess(workspace)
        assert access.authorize([candidate]) == (candidate.resolve(),), candidate
        probe = candidate / "probe.txt"
        assert access.resolve(str(probe)) == probe.resolve()

    # A drive/filesystem root is accepted on the same terms.
    root = Path(tmp_path.anchor)
    access = ExternalReadAccess(workspace)
    assert access.authorize([root]) == (root.resolve(),)
    assert access.resolve(str(tmp_path / "anywhere.txt")) == (
        tmp_path / "anywhere.txt"
    ).resolve()


def test_paths_that_do_not_exist_authorize_nothing(tmp_path: Path) -> None:
    access = ExternalReadAccess(_workspace(tmp_path))
    assert access.authorize([tmp_path / "missing"]) == ()
    assert access.is_available is False


def test_resolve_rejects_traversal_out_of_an_authorized_tree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    external.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    access.authorize([external])

    with pytest.raises(ValueError):
        access.resolve(str(external / ".." / "outside.txt"))
    with pytest.raises(ValueError):
        access.resolve(f"{external}\\..\\outside.txt")


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    """A link inside an authorized tree does not grant what it points at."""
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    external.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = external / "escape_link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    access = ExternalReadAccess(workspace)
    access.authorize([external])

    with pytest.raises(ValueError):
        access.resolve(str(link))


def test_resolve_rejects_relative_and_empty_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    external.mkdir()
    access = ExternalReadAccess(workspace)
    access.authorize([external])

    for raw in ("", "   ", "src/auth.py", None):
        with pytest.raises(ValueError):
            access.resolve(raw)


def test_resolve_without_authorization_always_fails(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    lonely = tmp_path / "lonely.txt"
    lonely.write_text("x", encoding="utf-8")

    access = ExternalReadAccess(workspace)
    with pytest.raises(ValueError):
        access.resolve(str(lonely))


def test_clear_and_workspace_switch_end_authorization(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "Foo-v1"
    external.mkdir()

    access = ExternalReadAccess(workspace)
    access.authorize([external])
    access.clear()
    assert access.is_available is False

    access.authorize([external])
    other = tmp_path / "Other-Project"
    other.mkdir()
    access.set_workspace_root(other)
    assert access.is_available is False


def test_authorizing_replaces_the_previous_turns_allowlist(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = tmp_path / "Alpha"
    second = tmp_path / "Beta"
    first.mkdir()
    second.mkdir()

    access = ExternalReadAccess(workspace)
    access.authorize([first])
    access.authorize([second])

    assert access.directories == (second.resolve(),)
    with pytest.raises(ValueError):
        access.resolve(str(first / "a.txt"))


# ── B. read_file through the live registry ───────────────────────────────────


def _registry_with_external(tmp_path: Path) -> tuple[ToolRegistry, Path, Path]:
    """Workspace plus an external folder holding a named file and a sibling."""
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    (workspace / "in_workspace.py").write_text("workspace_marker = 1\n", encoding="utf-8")

    folder = tmp_path / "My Reference Notes"
    folder.mkdir()
    named = folder / "design doc.md"
    named.write_text("# Design\nexternal_marker in the named file\n", encoding="utf-8")
    sibling = folder / "private notes.md"
    sibling.write_text("external_marker in the sibling\n", encoding="utf-8")

    return ToolRegistry(workspace_root=workspace), named, sibling


def test_named_external_markdown_file_with_spaces_reads_successfully(
    tmp_path: Path,
) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute("read_file", {"path": str(named)}, approval_cb=None)

    assert result.ok is True
    assert "external_marker in the named file" in result.payload["content"]
    assert result.payload["path"] == named.resolve().as_posix()
    assert result.payload["external"] is True
    assert result.payload["read_only"] is True
    assert result.payload["source"] == "external"


def test_a_sibling_file_stays_inaccessible(tmp_path: Path) -> None:
    registry, named, sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute("read_file", {"path": str(sibling)}, approval_cb=None)

    assert result.ok is False
    assert result.payload["failure_class"] == "path_error"
    assert "not authorized" in result.payload["error"]


def test_an_unauthorized_external_path_fails(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x", encoding="utf-8")
    registry.begin_external_read_turn([named])

    result = registry.execute("read_file", {"path": str(elsewhere)}, approval_cb=None)

    assert result.ok is False


def test_reads_with_no_authorization_at_all_fail(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)

    result = registry.execute("read_file", {"path": str(named)}, approval_cb=None)

    assert result.ok is False


def test_authorized_directory_grants_reads_anywhere_below_it(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    (external / "src" / "deep").mkdir(parents=True)
    target = external / "src" / "deep" / "auth.py"
    target.write_text("external_marker = 1\n" * 5, encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    result = registry.execute("read_file", {"path": str(target)}, approval_cb=None)
    assert result.ok is True
    assert result.payload["external"] is True

    window = registry.execute(
        "read_file", {"path": str(target), "offset": 2, "limit": 2}, approval_cb=None
    )
    assert window.ok is True
    assert window.payload["content"].count("external_marker") == 2
    assert window.payload["path"] == target.resolve().as_posix()


def test_a_directory_and_a_file_inside_it_both_work_in_one_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()
    inside = external / "README.md"
    inside.write_text("inside_marker\n", encoding="utf-8")
    other = external / "other.md"
    other.write_text("other_marker\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external, inside])

    assert registry.execute("read_file", {"path": str(inside)}, approval_cb=None).ok is True
    assert registry.execute("read_file", {"path": str(other)}, approval_cb=None).ok is True


def test_several_independent_external_paths_work_in_one_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    first = tmp_path / "Alpha"
    first.mkdir()
    (first / "a.md").write_text("alpha_marker\n", encoding="utf-8")
    second = tmp_path / "Beta"
    second.mkdir()
    (second / "b.md").write_text("beta_marker\n", encoding="utf-8")
    loose = tmp_path / "loose.md"
    loose.write_text("loose_marker\n", encoding="utf-8")
    unnamed = tmp_path / "unnamed.md"
    unnamed.write_text("unnamed_marker\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([first, second, loose])

    for path, marker in ((first / "a.md", "alpha_marker"), (second / "b.md", "beta_marker"), (loose, "loose_marker")):
        result = registry.execute("read_file", {"path": str(path)}, approval_cb=None)
        assert result.ok is True, path
        assert marker in result.payload["content"]

    assert registry.execute("read_file", {"path": str(unnamed)}, approval_cb=None).ok is False


def test_mixed_workspace_and_external_batch_validates_each_path(tmp_path: Path) -> None:
    registry, named, sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute(
        "read_file",
        {"paths": ["in_workspace.py", str(named), str(sibling)]},
        approval_cb=None,
    )

    assert result.ok is True
    files = result.payload["files"]
    workspace_entry = files["in_workspace.py"]
    assert workspace_entry["status"] == "complete"
    assert "external" not in workspace_entry

    external_entry = files[str(named)]
    assert external_entry["status"] == "complete"
    assert external_entry["external"] is True
    assert external_entry["read_only"] is True
    assert external_entry["path"] == named.resolve().as_posix()

    assert files[str(sibling)]["status"] == "error"


def test_results_do_not_name_other_authorized_locations(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    first = tmp_path / "Alpha"
    first.mkdir()
    (first / "a.md").write_text("alpha_marker\n", encoding="utf-8")
    secret_named = tmp_path / "Beta-Confidential"
    secret_named.mkdir()

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([first, secret_named])

    result = registry.execute("read_file", {"path": str(first / "a.md")}, approval_cb=None)

    assert result.ok is True
    assert "Beta-Confidential" not in str(result.payload)


def test_traversal_and_symlink_escapes_fail_through_read_file(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    traversal = registry.execute(
        "read_file", {"path": f"{external}\\..\\outside_secret.txt"}, approval_cb=None
    )
    assert traversal.ok is False

    link = external / "escape_link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    escaped = registry.execute("read_file", {"path": str(link)}, approval_cb=None)
    assert escaped.ok is False


# ── C. workspace reads are unchanged ─────────────────────────────────────────


def test_workspace_relative_reads_are_unchanged(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute("read_file", {"path": "in_workspace.py"}, approval_cb=None)

    assert result.ok is True
    assert result.payload["path"] == "in_workspace.py"
    assert "external" not in result.payload
    assert result.payload.get("source") is None


def test_workspace_relative_traversal_still_fails(tmp_path: Path) -> None:
    registry, named, sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute(
        "read_file", {"path": "../My Reference Notes/private notes.md"}, approval_cb=None
    )

    assert result.ok is False


def test_absolute_workspace_paths_stay_ordinary_workspace_reads(tmp_path: Path) -> None:
    registry, _named, _sibling = _registry_with_external(tmp_path)
    inside = registry.workspace_root / "in_workspace.py"

    result = registry.execute("read_file", {"path": str(inside)}, approval_cb=None)

    assert result.ok is True
    assert result.payload["path"] == "in_workspace.py"
    assert "external" not in result.payload


# ── D. grep_search ───────────────────────────────────────────────────────────


def test_grep_search_default_root_remains_the_workspace(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute(
        "grep_search", {"pattern": "workspace_marker"}, approval_cb=None
    )

    assert result.ok is True
    assert [m["path"] for m in result.payload["matches"]] == ["in_workspace.py"]
    assert "external" not in result.payload

    external_scan = registry.execute(
        "grep_search", {"pattern": "external_marker"}, approval_cb=None
    )
    assert external_scan.payload["matches"] == []


def test_grep_search_accepts_a_workspace_relative_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "mod.py").write_text("scoped_marker = 1\n", encoding="utf-8")
    (workspace / "other.py").write_text("scoped_marker = 2\n", encoding="utf-8")
    registry = ToolRegistry(workspace_root=workspace)

    result = registry.execute(
        "grep_search", {"pattern": "scoped_marker", "path": "pkg"}, approval_cb=None
    )

    assert result.ok is True
    assert [m["path"] for m in result.payload["matches"]] == ["pkg/mod.py"]


def test_grep_search_over_an_authorized_external_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    (external / "src").mkdir(parents=True)
    (external / "src" / "auth.py").write_text("external_marker = 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("external_marker = 2\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    result = registry.execute(
        "grep_search", {"pattern": "external_marker", "path": str(external)}, approval_cb=None
    )

    assert result.ok is True
    assert result.payload["external"] is True
    assert result.payload["read_only"] is True
    assert [m["path"] for m in result.payload["matches"]] == [
        (external / "src" / "auth.py").resolve().as_posix()
    ]
    # The returned path is one read_file accepts under the same authorization.
    follow_up = registry.execute(
        "read_file", {"path": result.payload["matches"][0]["path"]}, approval_cb=None
    )
    assert follow_up.ok is True


def test_grep_search_over_an_authorized_external_file_is_limited_to_it(
    tmp_path: Path,
) -> None:
    registry, named, sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute(
        "grep_search", {"pattern": "external_marker", "path": str(named)}, approval_cb=None
    )

    assert result.ok is True
    assert [m["path"] for m in result.payload["matches"]] == [
        named.resolve().as_posix()
    ]
    assert sibling.name not in str(result.payload)


def test_grep_search_rejects_an_unauthorized_external_scope(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    result = registry.execute(
        "grep_search", {"pattern": "external_marker", "path": str(tmp_path)}, approval_cb=None
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "search_scope_unauthorized"


def test_grep_search_preserves_pattern_and_result_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()
    (external / "a.py").write_text("def handle_alpha():\n    pass\n", encoding="utf-8")
    (external / "b.txt").write_text("handle_alpha mentioned\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    result = registry.execute(
        "grep_search",
        {
            "pattern": r"^def handle_alpha",
            "path": str(external),
            "include_pattern": "**/*.py",
            "case_sensitive": True,
            "max_results": 5,
        },
        approval_cb=None,
    )

    assert result.ok is True
    assert result.payload["regex_mode"] is True
    assert result.payload["include_pattern"] == "**/*.py"
    assert result.payload["truncated"] is False
    assert "summary" in result.payload
    assert [m["path"] for m in result.payload["matches"]] == [
        (external / "a.py").resolve().as_posix()
    ]


def test_search_codebase_no_longer_offers_an_external_source(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()
    (external / "only.py").write_text("external_marker = True\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    result = registry.execute(
        "search_codebase", {"query": "external_marker", "source": "reference"}, approval_cb=None
    )

    assert result.ok is False
    assert "grep_search" in result.payload["error"]


# ── E. nothing here reaches a mutation or execution path ─────────────────────


def test_apply_patch_cannot_write_to_an_authorized_external_location(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()
    victim = external / "target.txt"
    victim.write_text("original\n", encoding="utf-8")

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    def approve(_request):
        from aura.conversation.tools._types import ApprovalDecision

        return ApprovalDecision(action="approve")

    for args in (
        {"operation": "replace", "path": str(victim), "content": "pwned"},
        {"operation": "create", "path": str(external / "new.txt"), "content": "pwned"},
        {"operation": "delete", "path": str(victim)},
    ):
        result = registry.execute("apply_patch", args, approval_cb=approve)
        assert result.ok is False, args

    assert victim.read_text(encoding="utf-8") == "original\n"
    assert not (external / "new.txt").exists()


def test_resolve_in_root_never_consults_the_allowlist(tmp_path: Path) -> None:
    """Mutation paths resolve through the jail, which has no external route."""
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    with pytest.raises(ValueError):
        registry._resolve_in_root(str(named))


def test_shell_cwd_stays_workspace_relative(tmp_path: Path) -> None:
    from aura.project_env import resolve_workspace_cwd

    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()

    registry = ToolRegistry(workspace_root=workspace)
    registry.begin_external_read_turn([external])

    with pytest.raises(ValueError):
        resolve_workspace_cwd(workspace, str(external))


# ── F. catalog and lifecycle ─────────────────────────────────────────────────


def _tool_names(tool_defs: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in tool_defs}


def test_read_reference_file_is_gone_from_every_live_catalog(tmp_path: Path) -> None:
    from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS
    from aura.conversation.tools.registry import TOOL_HANDLERS

    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()

    for read_only in (False, True):
        registry = ToolRegistry(workspace_root=workspace, read_only=read_only)
        registry.begin_external_read_turn([external])
        assert "read_reference_file" not in _tool_names(registry.tool_defs())

    catalog = ToolCatalog()
    for read_only in (False, True):
        assert "read_reference_file" not in _tool_names(
            catalog.build_tool_defs(read_only=read_only)
        )

    assert "read_reference_file" not in TOOL_HANDLERS
    assert "read_reference_file" not in BUILTIN_TOOL_EFFECTS


def test_authorization_does_not_move_the_tool_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "Foo-v2"
    workspace.mkdir()
    external = tmp_path / "Foo-v1"
    external.mkdir()

    registry = ToolRegistry(workspace_root=workspace)
    before = registry.tool_defs()
    registry.begin_external_read_turn([external])
    during = registry.tool_defs()
    registry.clear_external_read_authorization()
    after = registry.tool_defs()

    assert before == during == after


def test_clearing_authorization_ends_external_reads(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])
    assert registry.external_read_available is True
    assert registry.external_read_names == (named.name,)

    registry.clear_external_read_authorization()

    assert registry.external_read_available is False
    assert registry.execute("read_file", {"path": str(named)}, approval_cb=None).ok is False


def test_workspace_switch_clears_authorization(tmp_path: Path) -> None:
    registry, named, _sibling = _registry_with_external(tmp_path)
    registry.begin_external_read_turn([named])

    other_workspace = tmp_path / "Other-Project"
    other_workspace.mkdir()
    registry.set_workspace_root(other_workspace)

    assert registry.external_read_available is False
    assert registry.execute("read_file", {"path": str(named)}, approval_cb=None).ok is False
