"""AgentStore — the one owner of agent-definition discovery and lifecycle.

Definitions live in exactly two places, and the location is the scope:

* project — ``<workspace>/.aura/agents/definitions/<agent_id>.md``
* personal — ``<data_dir>/agents/definitions/<agent_id>.md``

The store discovers both, reads each file through
:func:`aura.agents.document.parse_agent_document`, and reports a file it
could not load as a visible, fixable row rather than letting it disappear.
Ids are global across the two scopes: a project and a personal definition
that claim the same id are both refused, because silently preferring one
would mean an agent's identity depended on where the reader happened to be
standing.

Nothing here decides what an agent may do. Permission is private, per user,
per workspace, and lives in :mod:`aura.agents.local_state`.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from aura.agents.document import parse_agent_document, render_agent_document
from aura.agents.identity import AgentScope, is_valid_agent_id, new_agent_id
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.validation import agent_name_error, delegation_description_error
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir, first_link_like_component, is_link_like

logger = logging.getLogger(__name__)

_DEFINITION_SUFFIX = ".md"


class AgentStoreError(RuntimeError):
    """A create, update, or delete that could not be carried out."""


@dataclass(frozen=True)
class AgentSummary:
    """One discovered definition, valid or not.

    An invalid entry keeps its id and scope — it is addressable, so the user
    can fix the file or delete the row — but carries no definition, and is
    never offered to Aura.
    """

    agent_id: str
    scope: AgentScope
    name: str
    description: str
    valid: bool
    errors: tuple[str, ...] = ()
    definition: AgentDefinition | None = None
    source: Path | None = None

    @property
    def scope_label(self) -> str:
        return self.scope.label


class AgentStore:
    """Discovery and CRUD for project and personal agent definitions.

    ``personal_dir`` exists for test isolation, exactly as
    :class:`aura.skills.library.SkillLibrary` uses it: a test that overrides
    it never touches the developer's real personal agents.
    """

    def __init__(self, workspace_root: Path | str, *, personal_dir: Path | None = None) -> None:
        self._workspace_root = Path(workspace_root)
        self._personal_dir = (
            Path(personal_dir)
            if personal_dir is not None
            else data_dir() / "agents" / "definitions"
        )

    # ---- locations ---------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        return self._workspace_root / ".aura" / "agents" / "definitions"

    @property
    def personal_dir(self) -> Path:
        return self._personal_dir

    def directory(self, scope: AgentScope) -> Path:
        return self.project_dir if scope is AgentScope.PROJECT else self._personal_dir

    def path_for(self, scope: AgentScope, agent_id: str) -> Path:
        safe_id = self._require_agent_id(agent_id)
        path = self.directory(scope) / f"{safe_id}{_DEFINITION_SUFFIX}"
        self._require_safe_storage_path(path, action="address")
        return path

    # ---- discovery ---------------------------------------------------------

    def list_summaries(self) -> tuple[AgentSummary, ...]:
        """Every discovered definition, project first, then personal.

        Within a scope, rows are ordered by display name so a renamed agent
        moves where its name says it should, and an unreadable file sits with
        the rest instead of at the end.
        """
        found: list[AgentSummary] = []
        for scope in (AgentScope.PROJECT, AgentScope.PERSONAL):
            found.extend(self._read_scope(scope))

        duplicates = _duplicate_ids(summary.agent_id for summary in found)
        rows = [_reject_duplicate(row, duplicates) for row in found]
        rows.sort(key=lambda row: (row.scope is AgentScope.PERSONAL, row.name.lower()))
        return tuple(rows)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        """Only the definitions that loaded cleanly, in list order."""
        return tuple(
            row.definition for row in self.list_summaries() if row.definition is not None
        )

    def get(self, agent_id: str) -> AgentDefinition | None:
        """The one valid definition with *agent_id*, or None."""
        self._require_agent_id(agent_id)
        row = self.summary(agent_id)
        return row.definition if row is not None else None

    def summary(self, agent_id: str) -> AgentSummary | None:
        self._require_agent_id(agent_id)
        return next((row for row in self.list_summaries() if row.agent_id == agent_id), None)

    def summary_in_scope(
        self, scope: AgentScope, agent_id: str
    ) -> AgentSummary | None:
        """The exact row at ``scope/id``, never an ambiguous cross-scope match."""
        self._require_agent_id(agent_id)
        return next(
            (
                row
                for row in self.list_summaries()
                if row.scope is scope and row.agent_id == agent_id
            ),
            None,
        )

    def _read_scope(self, scope: AgentScope) -> list[AgentSummary]:
        directory = self.directory(scope)
        try:
            self._require_safe_storage_path(directory, action="discover")
        except AgentStoreError:
            logger.warning("agents: refusing linked definition storage at %s", directory)
            return []
        try:
            entries = sorted(directory.glob(f"*{_DEFINITION_SUFFIX}"))
        except OSError:
            logger.debug("agents: could not list %s", directory, exc_info=True)
            return []

        rows: list[AgentSummary] = []
        for path in entries:
            try:
                self._require_safe_storage_path(path, action="discover")
            except AgentStoreError:
                continue
            if not path.is_file() or is_link_like(path):
                continue
            rows.append(self._read_file(path, scope))
        return rows

    def _read_file(self, path: Path, scope: AgentScope) -> AgentSummary:
        agent_id = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return AgentSummary(
                agent_id=agent_id,
                scope=scope,
                name=agent_id,
                description="",
                valid=False,
                errors=(f"could not be read: {exc}",),
                source=path,
            )

        parsed = parse_agent_document(raw, scope=scope, expected_id=agent_id)
        if parsed.definition is None:
            return AgentSummary(
                agent_id=agent_id,
                scope=scope,
                name=agent_id,
                description="",
                valid=False,
                errors=parsed.errors,
                source=path,
            )
        definition = parsed.definition
        return AgentSummary(
            agent_id=definition.agent_id,
            scope=scope,
            name=definition.name,
            description=definition.description,
            valid=True,
            definition=definition,
            source=path,
        )

    # ---- lifecycle ---------------------------------------------------------

    def create(
        self,
        scope: AgentScope,
        *,
        name: str,
        description: str,
        instructions: str,
        provider: str = "",
        model: str = "",
        thinking: AgentThinking = AgentThinking.INHERIT,
    ) -> AgentDefinition:
        """Mint an id and write a new definition into *scope*'s directory."""
        definition = AgentDefinition(
            agent_id=new_agent_id(),
            scope=scope,
            name=str(name or "").strip(),
            description=str(description or "").strip(),
            instructions=str(instructions or "").strip(),
            provider=str(provider or "").strip(),
            model=str(model or "").strip(),
            thinking=thinking,
        )
        self._validate(definition)
        path = self.path_for(scope, definition.agent_id)
        if path.exists():
            raise AgentStoreError("An agent definition already exists under that id.")
        self._write(path, definition)
        return definition

    def update(self, definition: AgentDefinition) -> AgentDefinition:
        """Overwrite an existing definition in place, keeping its id and scope."""
        self._require_agent_id(definition.agent_id)
        self._validate(definition)
        path = self.path_for(definition.scope, definition.agent_id)
        if not path.is_file():
            raise AgentStoreError("That agent no longer exists on disk.")
        self._write(path, definition)
        return definition

    def delete(self, scope: AgentScope, agent_id: str) -> bool:
        """Remove exactly the definition at ``scope/id``."""
        self._require_agent_id(agent_id)
        # Discovery intentionally hides redirected storage. Deletion must be
        # stricter: returning "not found" through a linked scope would make a
        # refused delete look successful to its caller.
        for candidate_scope in (AgentScope.PROJECT, AgentScope.PERSONAL):
            self._require_safe_storage_path(
                self.directory(candidate_scope), action="delete"
            )
        row = self.summary_in_scope(scope, agent_id)
        if row is None or row.source is None:
            return False
        self._require_safe_storage_path(row.source, action="delete")
        try:
            row.source.unlink()
        except OSError as exc:
            raise AgentStoreError(f"Could not delete that agent: {exc}") from exc
        return True

    def _write(self, path: Path, definition: AgentDefinition) -> None:
        try:
            self._require_agent_id(definition.agent_id)
            self._require_safe_storage_path(path, action="write")
            path.parent.mkdir(parents=True, exist_ok=True)
            # mkdir may have raced with a link/junction insertion. Recheck the
            # complete chain immediately before the atomic replacement.
            self._require_safe_storage_path(path, action="write")
            atomic_write_bytes(path, render_agent_document(definition).encode("utf-8"))
        except AgentStoreError:
            raise
        except OSError as exc:
            raise AgentStoreError(f"Could not save that agent: {exc}") from exc

    def _validate(self, definition: AgentDefinition) -> None:
        self._require_agent_id(definition.agent_id)
        name_error = agent_name_error(definition.name)
        if name_error:
            raise AgentStoreError(name_error)
        description_error = delegation_description_error(definition.description)
        if description_error:
            raise AgentStoreError(description_error)
        if not definition.instructions:
            raise AgentStoreError("An agent needs instructions.")
        collision = any(
            row.agent_id == definition.agent_id and row.scope is not definition.scope
            for row in self.list_summaries()
        )
        if collision:
            raise AgentStoreError(
                f"Another agent already uses the id {definition.agent_id}."
            )

    @staticmethod
    def _require_agent_id(agent_id: object) -> str:
        if not is_valid_agent_id(agent_id):
            raise AgentStoreError(f"'{agent_id}' is not a valid immutable agent id.")
        return str(agent_id)

    @staticmethod
    def _require_safe_storage_path(path: Path, *, action: str) -> None:
        """Refuse every redirecting component from the volume root downward."""
        absolute = Path(path).absolute()
        anchor = Path(absolute.anchor)
        relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        linked = first_link_like_component(anchor, tuple(relative_parts))
        if linked is not None:
            raise AgentStoreError(
                f"Could not {action} Agent definitions through a symlink, junction, "
                f"or redirecting reparse point ({linked})."
            )


def _duplicate_ids(ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for agent_id in ids:
        if agent_id in seen:
            duplicates.add(agent_id)
        seen.add(agent_id)
    return duplicates


def _reject_duplicate(row: AgentSummary, duplicates: set[str]) -> AgentSummary:
    """Refuse both sides of a duplicate id rather than picking a winner."""
    if row.agent_id not in duplicates:
        return row
    message = (
        f"the id {row.agent_id} is claimed by more than one definition — "
        "ids are unique across project and personal agents"
    )
    return AgentSummary(
        agent_id=row.agent_id,
        scope=row.scope,
        name=row.name,
        description=row.description,
        valid=False,
        errors=(*row.errors, message),
        definition=None,
        source=row.source,
    )


__all__ = ["AgentStore", "AgentStoreError", "AgentSummary"]
