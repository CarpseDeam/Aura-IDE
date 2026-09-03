"""Agent definitions on disk: identity, scope, duplicates, and CRUD.

An agent's identity is the opaque id minted at creation. It survives every
rename, it is what the file is named, and it is unique across both scopes —
a project and a personal definition that claim the same id are both refused
rather than silently resolved in one direction.

A definition also never carries authority. Permission is private local state
(see ``test_agents_local_state``), and a file that tries to declare any is
rejected outright, so a repository cannot ship an agent that appears to
grant itself something.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aura.agents.document import parse_agent_document, render_agent_document
from aura.agents.identity import AgentScope, is_valid_agent_id, new_agent_id
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.store import AgentStore, AgentStoreError


@pytest.fixture()
def store(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")


def _write(directory: Path, agent_id: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _definition(agent_id: str, *, name: str = "Reviewer") -> str:
    return (
        f"---\nid: {agent_id}\nname: {name}\n"
        "description: Reviews a diff for defects.\nthinking: inherit\n---\n\n"
        "Read the diff and report only what you can demonstrate.\n"
    )


# ── identity ─────────────────────────────────────────────────────────────────


def test_new_ids_are_opaque_unique_and_file_safe() -> None:
    ids = {new_agent_id() for _ in range(50)}

    assert len(ids) == 50
    assert all(is_valid_agent_id(agent_id) for agent_id in ids)
    assert not any({"/", "\\", ":", "."} & set(agent_id) for agent_id in ids)


@pytest.mark.parametrize(
    "raw",
    ["", "no", "../escape", "has/slash", "has\\slash", "UPPER", ".hidden", "a" * 65],
)
def test_unsafe_ids_are_refused(raw: str) -> None:
    assert is_valid_agent_id(raw) is False


def test_the_id_survives_a_rename(store: AgentStore) -> None:
    created = store.create(
        AgentScope.PROJECT,
        name="Reviewer",
        description="Reviews a diff.",
        instructions="Look closely.",
    )

    store.update(
        AgentDefinition(
            agent_id=created.agent_id,
            scope=AgentScope.PROJECT,
            name="Auditor",
            description="Audits a diff.",
            instructions="Look closely.",
        )
    )

    reloaded = store.get(created.agent_id)
    assert reloaded is not None
    assert reloaded.name == "Auditor"
    assert reloaded.agent_id == created.agent_id
    assert store.path_for(AgentScope.PROJECT, created.agent_id).is_file()


def test_display_names_are_never_identity(store: AgentStore) -> None:
    """Two agents may share a display name; they are still different agents."""
    first = store.create(
        AgentScope.PROJECT, name="Reviewer", description="One.", instructions="A."
    )
    second = store.create(
        AgentScope.PERSONAL, name="Reviewer", description="Two.", instructions="B."
    )

    assert first.agent_id != second.agent_id
    assert {row.agent_id for row in store.list_summaries()} == {
        first.agent_id,
        second.agent_id,
    }
    assert all(row.valid for row in store.list_summaries())


def test_a_declared_id_must_match_its_file_name(store: AgentStore) -> None:
    _write(store.project_dir, "aaaabbbbccccdddd", _definition("eeeeffffgggghhhh"))

    (row,) = store.list_summaries()
    assert row.valid is False
    assert any("does not match the file name" in message for message in row.errors)


# ── scope comes from location ────────────────────────────────────────────────


def test_scope_is_derived_from_the_directory(store: AgentStore) -> None:
    _write(store.project_dir, "projectagent", _definition("projectagent", name="In project"))
    _write(store.personal_dir, "personalagent", _definition("personalagent", name="In personal"))

    scopes = {row.agent_id: row.scope for row in store.list_summaries()}

    assert scopes == {
        "projectagent": AgentScope.PROJECT,
        "personalagent": AgentScope.PERSONAL,
    }


def test_definitions_live_where_the_layout_says(store: AgentStore, tmp_path: Path) -> None:
    project = store.create(
        AgentScope.PROJECT, name="P", description="Project agent.", instructions="Do."
    )
    personal = store.create(
        AgentScope.PERSONAL, name="U", description="Personal agent.", instructions="Do."
    )

    assert store.project_dir == tmp_path / "workspace" / ".aura" / "agents" / "definitions"
    assert (store.project_dir / f"{project.agent_id}.md").is_file()
    assert (store.personal_dir / f"{personal.agent_id}.md").is_file()
    # A personal definition never lands inside the project.
    assert not (store.project_dir / f"{personal.agent_id}.md").exists()


# ── duplicates are errors ────────────────────────────────────────────────────


def test_a_duplicate_id_across_scopes_refuses_both_sides(store: AgentStore) -> None:
    _write(store.project_dir, "sharedagentid", _definition("sharedagentid", name="From project"))
    _write(store.personal_dir, "sharedagentid", _definition("sharedagentid", name="From personal"))

    rows = store.list_summaries()

    assert len(rows) == 2
    assert [row.valid for row in rows] == [False, False]
    assert all(any("more than one definition" in msg for msg in row.errors) for row in rows)
    assert store.definitions() == ()
    assert store.get("sharedagentid") is None


def test_writing_over_a_foreign_scope_is_refused(store: AgentStore) -> None:
    _write(store.personal_dir, "claimedagentid", _definition("claimedagentid"))

    with pytest.raises(AgentStoreError):
        store.update(
            AgentDefinition(
                agent_id="claimedagentid",
                scope=AgentScope.PROJECT,
                name="Impostor",
                description="Tries to take an id.",
                instructions="No.",
            )
        )


# ── definitions carry no authority ───────────────────────────────────────────


@pytest.mark.parametrize("key", ["permission", "permissions", "grants", "terminal"])
def test_a_definition_may_not_declare_permission(store: AgentStore, key: str) -> None:
    _write(
        store.project_dir,
        "grabbyagentid",
        "---\nid: grabbyagentid\nname: Grabby\ndescription: Wants more.\n"
        f"{key}: worktree_edit_terminal\n---\n\nDo work.\n",
    )

    (row,) = store.list_summaries()

    assert row.valid is False
    assert any("is not allowed in a definition" in msg for msg in row.errors)
    assert any("granted locally" in msg for msg in row.errors)


def test_a_saved_definition_declares_nothing_about_authority(store: AgentStore) -> None:
    created = store.create(
        AgentScope.PROJECT, name="Reviewer", description="Reviews.", instructions="Read."
    )

    text = store.path_for(AgentScope.PROJECT, created.agent_id).read_text(encoding="utf-8")

    assert "permission" not in text.lower()
    assert "worktree" not in text.lower()


# ── the file format ──────────────────────────────────────────────────────────


def test_the_file_is_readable_markdown_with_the_body_as_instructions(
    store: AgentStore,
) -> None:
    created = store.create(
        AgentScope.PERSONAL,
        name="Scout",
        description="Finds the relevant code.",
        instructions="# Scout\n\nSearch broadly, report narrowly.",
        model="claude-sonnet-4-6",
        thinking=AgentThinking.MAX,
    )

    text = store.path_for(AgentScope.PERSONAL, created.agent_id).read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert f"id: {created.agent_id}" in text
    assert "name: Scout" in text
    assert "model: claude-sonnet-4-6" in text
    assert "thinking: max" in text
    assert text.rstrip().endswith("Search broadly, report narrowly.")

    reloaded = store.get(created.agent_id)
    assert reloaded is not None
    assert reloaded.instructions == "# Scout\n\nSearch broadly, report narrowly."
    assert reloaded.model == "claude-sonnet-4-6"
    assert reloaded.thinking is AgentThinking.MAX


def test_a_definition_can_name_a_provider_without_machine_configuration(
    store: AgentStore,
) -> None:
    created = store.create(
        AgentScope.PROJECT,
        name="Named",
        description="Names a qualified model target.",
        instructions="Work.",
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    text = store.path_for(AgentScope.PROJECT, created.agent_id).read_text(encoding="utf-8")

    assert "provider: anthropic" in text
    assert "model: claude-sonnet-4-6" in text


def test_naming_no_model_omits_the_line_entirely(store: AgentStore) -> None:
    created = store.create(
        AgentScope.PROJECT, name="Plain", description="Uses Aura's.", instructions="Work."
    )

    text = store.path_for(AgentScope.PROJECT, created.agent_id).read_text(encoding="utf-8")

    assert "model:" not in text
    assert "provider:" not in text
    assert created.provider == ""
    assert created.model == ""
    assert created.model_label == "Aura's current model"


def test_an_existing_provider_line_is_preserved_as_the_qualified_target(
    store: AgentStore,
) -> None:
    _write(
        store.project_dir,
        "legacyagentid",
        "---\nid: legacyagentid\nname: Legacy\ndescription: d.\n"
        "provider: anthropic\nmodel: claude-sonnet-4-6\n---\n\nWork.\n",
    )

    (row,) = store.list_summaries()

    assert row.valid is True
    assert row.definition is not None
    assert row.definition.provider == "anthropic"
    assert row.definition.model == "claude-sonnet-4-6"

    # Writing it back retains only the portable ids, never local credentials.
    store.update(row.definition)
    text = store.path_for(AgentScope.PROJECT, "legacyagentid").read_text(encoding="utf-8")
    assert "provider: anthropic" in text


def test_parsing_does_not_require_the_target_provider_on_this_machine() -> None:
    parsed = parse_agent_document(
        "---\nid: portableagent\nname: Portable\ndescription: d.\n"
        "provider: future-provider\nmodel: future-model\n---\n\nWork.\n",
        scope=AgentScope.PROJECT,
        expected_id="portableagent",
    )

    assert parsed.ok
    assert parsed.definition is not None
    assert (parsed.definition.provider, parsed.definition.model) == (
        "future-provider",
        "future-model",
    )


@pytest.mark.parametrize(
    ("provider", "model"),
    [("claude_code", "claude-code"), ("codex", "codex")],
)
def test_retired_cli_agent_target_pair_migrates_to_inherit(
    provider: str, model: str
) -> None:
    parsed = parse_agent_document(
        "---\nid: legacycliagent\nname: Legacy CLI\ndescription: d.\n"
        f"provider: {provider}\nmodel: {model}\n---\n\nWork.\n",
        scope=AgentScope.PROJECT,
        expected_id="legacycliagent",
    )

    assert parsed.ok
    assert parsed.definition is not None
    assert (parsed.definition.provider, parsed.definition.model) == ("", "")
    rewritten = render_agent_document(parsed.definition)
    assert "provider:" not in rewritten
    assert "model:" not in rewritten


def test_retired_cli_model_without_provider_keeps_model_only_compatibility() -> None:
    parsed = parse_agent_document(
        "---\nid: legacymodelonly\nname: Legacy model\ndescription: d.\n"
        "model: codex\n---\n\nWork.\n",
        scope=AgentScope.PROJECT,
        expected_id="legacymodelonly",
    )

    assert parsed.ok
    assert parsed.definition is not None
    assert (parsed.definition.provider, parsed.definition.model) == ("", "codex")


def test_render_and_parse_round_trip() -> None:
    definition = AgentDefinition(
        agent_id="roundtripagent",
        scope=AgentScope.PROJECT,
        name="Round Trip",
        description="Goes out and comes back.",
        instructions="Stay the same.",
        provider="openai",
        model="gpt-5.5",
        thinking=AgentThinking.OFF,
    )

    parsed = parse_agent_document(
        render_agent_document(definition),
        scope=AgentScope.PROJECT,
        expected_id="roundtripagent",
    )

    assert parsed.ok
    assert parsed.definition == definition


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no front matter at all\n",
        "---\nid: unclosedagent\nname: X\n",
        "---\n[]\n---\n\nbody\n",
        "---\nname: No Id\ndescription: d.\n---\n\nbody\n",
        "---\nid: bodylessagent\nname: X\ndescription: d.\n---\n",
    ],
)
def test_malformed_definitions_report_instead_of_disappearing(
    store: AgentStore, raw: str
) -> None:
    _write(store.project_dir, "brokenagentid", raw)

    (row,) = store.list_summaries()

    assert row.valid is False
    assert row.errors
    assert row.agent_id == "brokenagentid"
    assert store.definitions() == ()


# ── CRUD ─────────────────────────────────────────────────────────────────────


def test_create_read_update_delete(store: AgentStore) -> None:
    created = store.create(
        AgentScope.PROJECT,
        name="Reviewer",
        description="Reviews a diff.",
        instructions="Read it.",
    )
    assert store.get(created.agent_id) == created

    store.update(
        AgentDefinition(
            agent_id=created.agent_id,
            scope=AgentScope.PROJECT,
            name="Reviewer",
            description="Reviews a diff.",
            instructions="Read it twice.",
            thinking=AgentThinking.HIGH,
        )
    )
    updated = store.get(created.agent_id)
    assert updated is not None
    assert updated.instructions == "Read it twice."
    assert updated.thinking is AgentThinking.HIGH

    assert store.delete(AgentScope.PROJECT, created.agent_id) is True
    assert store.get(created.agent_id) is None
    assert store.list_summaries() == ()
    assert store.delete(AgentScope.PROJECT, created.agent_id) is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "description": "d", "instructions": "i"},
        {"name": "n", "description": "", "instructions": "i"},
        {"name": "n", "description": "d", "instructions": ""},
    ],
)
def test_an_incomplete_agent_is_never_written(store: AgentStore, kwargs: dict) -> None:
    with pytest.raises(AgentStoreError):
        store.create(AgentScope.PROJECT, **kwargs)

    assert store.list_summaries() == ()


def test_updating_an_agent_that_is_gone_is_refused(store: AgentStore) -> None:
    created = store.create(
        AgentScope.PROJECT, name="Gone", description="d", instructions="i"
    )
    store.delete(AgentScope.PROJECT, created.agent_id)

    with pytest.raises(AgentStoreError):
        store.update(created)


def test_listing_is_project_first_then_by_name(store: AgentStore) -> None:
    store.create(AgentScope.PERSONAL, name="alpha", description="d", instructions="i")
    store.create(AgentScope.PROJECT, name="zulu", description="d", instructions="i")
    store.create(AgentScope.PROJECT, name="bravo", description="d", instructions="i")

    rows = store.list_summaries()

    assert [(row.scope, row.name) for row in rows] == [
        (AgentScope.PROJECT, "bravo"),
        (AgentScope.PROJECT, "zulu"),
        (AgentScope.PERSONAL, "alpha"),
    ]


def test_an_empty_workspace_has_no_agents(store: AgentStore) -> None:
    assert store.list_summaries() == ()
    assert store.definitions() == ()
    assert store.get("anything") is None
