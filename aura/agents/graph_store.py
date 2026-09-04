"""AgentGraphStore — the one owner of workflow discovery and lifecycle.

Workflows live in exactly two places, and the location is the scope:

* project — ``<workspace>/.aura/agents/workflows/<graph_id>.json``
* personal — ``<data_dir>/agents/workflows/<graph_id>.json``

The store discovers both, reads each file through
:func:`aura.agents.graph_document.parse_graph_document`, and reports a file
it could not load as a visible, fixable row rather than letting it
disappear. Ids are global across the two scopes: a project and a personal
workflow that claim the same id are both refused, because silently
preferring one would mean a workflow's identity depended on where the reader
happened to be standing. This is deliberately the same contract
:class:`aura.agents.store.AgentStore` gives definitions.

Nothing here decides whether Aura may call a workflow. That is private, per
user, per workspace, and lives in :mod:`aura.agents.graph_local_state`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from aura.agents.graph_document import parse_graph_document, render_graph_document
from aura.agents.graph_models import (
    WorkflowGraph,
    is_valid_graph_id,
    new_graph,
)
from aura.agents.graph_validation import reference_scope_error
from aura.agents.identity import AgentScope
from aura.agents.validation import workflow_name_error
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir, first_link_like_component, is_link_like

logger = logging.getLogger(__name__)

_WORKFLOW_SUFFIX = ".json"

#: What a workflow is called before anyone renames it.
NEW_WORKFLOW_NAME = "New workflow"


class AgentGraphStoreError(RuntimeError):
    """A create, save, or delete that could not be carried out."""


@dataclass(frozen=True)
class WorkflowSummary:
    """One discovered workflow, valid or not.

    An invalid entry keeps its id and scope — it is addressable, so the user
    can fix the file or delete the row — but carries no graph.
    """

    graph_id: str
    scope: AgentScope
    name: str
    description: str
    valid: bool
    errors: tuple[str, ...] = ()
    graph: WorkflowGraph | None = None
    source: Path | None = None

    @property
    def scope_label(self) -> str:
        return self.scope.label


class AgentGraphStore:
    """Discovery and CRUD for project and personal workflow graphs.

    ``personal_dir`` exists for test isolation, exactly as
    :class:`aura.agents.store.AgentStore` uses it. ``agent_scopes`` is how
    the store learns which agents exist and where they live; when it is
    given, a save that would put a personal agent into a project workflow is
    refused at the door rather than written and flagged afterwards.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        personal_dir: Path | None = None,
        agent_scopes: Callable[[], Mapping[str, AgentScope]] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._personal_dir = (
            Path(personal_dir)
            if personal_dir is not None
            else data_dir() / "agents" / "workflows"
        )
        self._agent_scopes = agent_scopes

    # ---- locations ---------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        return self._workspace_root / ".aura" / "agents" / "workflows"

    @property
    def personal_dir(self) -> Path:
        return self._personal_dir

    def directory(self, scope: AgentScope) -> Path:
        return self.project_dir if scope is AgentScope.PROJECT else self._personal_dir

    def path_for(self, scope: AgentScope, graph_id: str) -> Path:
        safe_id = self._require_graph_id(graph_id)
        path = self.directory(scope) / f"{safe_id}{_WORKFLOW_SUFFIX}"
        self._require_safe_storage_path(path, action="address")
        return path

    # ---- discovery ---------------------------------------------------------

    def list_summaries(self) -> tuple[WorkflowSummary, ...]:
        """Every discovered workflow, project first, then personal."""
        found: list[WorkflowSummary] = []
        for scope in (AgentScope.PROJECT, AgentScope.PERSONAL):
            found.extend(self._read_scope(scope))

        duplicates = _duplicate_ids(row.graph_id for row in found)
        rows = [_reject_duplicate(row, duplicates) for row in found]
        rows.sort(key=lambda row: (row.scope is AgentScope.PERSONAL, row.name.lower()))
        return tuple(rows)

    def graphs(self) -> tuple[WorkflowGraph, ...]:
        """Only the workflows that loaded cleanly, in list order."""
        return tuple(row.graph for row in self.list_summaries() if row.graph is not None)

    def get(self, graph_id: str) -> WorkflowGraph | None:
        """The one valid workflow with *graph_id*, or None."""
        row = self.summary(graph_id)
        return row.graph if row is not None else None

    def summary(self, graph_id: str) -> WorkflowSummary | None:
        self._require_graph_id(graph_id)
        return next(
            (row for row in self.list_summaries() if row.graph_id == graph_id), None
        )

    def summary_in_scope(
        self, scope: AgentScope, graph_id: str
    ) -> WorkflowSummary | None:
        """The exact row at ``scope/id``, never an ambiguous cross-scope match."""
        self._require_graph_id(graph_id)
        return next(
            (
                row
                for row in self.list_summaries()
                if row.scope is scope and row.graph_id == graph_id
            ),
            None,
        )

    def _read_scope(self, scope: AgentScope) -> list[WorkflowSummary]:
        directory = self.directory(scope)
        try:
            self._require_safe_storage_path(directory, action="discover")
        except AgentGraphStoreError:
            logger.warning("agents: refusing linked workflow storage at %s", directory)
            return []
        try:
            entries = sorted(directory.glob(f"*{_WORKFLOW_SUFFIX}"))
        except OSError:
            logger.debug("agents: could not list %s", directory, exc_info=True)
            return []

        rows: list[WorkflowSummary] = []
        for path in entries:
            try:
                self._require_safe_storage_path(path, action="discover")
            except AgentGraphStoreError:
                continue
            if not path.is_file() or is_link_like(path):
                continue
            rows.append(self._read_file(path, scope))
        return rows

    def _read_file(self, path: Path, scope: AgentScope) -> WorkflowSummary:
        graph_id = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return WorkflowSummary(
                graph_id=graph_id,
                scope=scope,
                name=graph_id,
                description="",
                valid=False,
                errors=(f"could not be read: {exc}",),
                source=path,
            )

        parsed = parse_graph_document(raw, scope=scope, expected_id=graph_id)
        if parsed.graph is None:
            return WorkflowSummary(
                graph_id=graph_id,
                scope=scope,
                name=graph_id,
                description="",
                valid=False,
                errors=parsed.errors,
                source=path,
            )
        graph = parsed.graph
        return WorkflowSummary(
            graph_id=graph.graph_id,
            scope=scope,
            name=graph.name,
            description=graph.description,
            valid=True,
            graph=graph,
            source=path,
        )

    # ---- lifecycle ---------------------------------------------------------

    def create(
        self, scope: AgentScope, *, name: str = NEW_WORKFLOW_NAME, description: str = ""
    ) -> WorkflowGraph:
        """Mint an id and write a new workflow with its two fixed ends."""
        graph = new_graph(scope, name, description=description)
        self._validate(graph)
        path = self.path_for(scope, graph.graph_id)
        if path.exists():
            raise AgentGraphStoreError("A workflow already exists under that id.")
        self._write(path, graph)
        return graph

    def create_supplied(self, graph: WorkflowGraph) -> WorkflowGraph:
        """Create an exact immutable graph without minting or overwriting.

        An identical graph already present at the same scope is a successful
        retry. Any different claim on its id is refused.
        """
        if not isinstance(graph, WorkflowGraph):
            raise AgentGraphStoreError("A supplied Workflow must be a WorkflowGraph.")
        self._validate(graph)
        matches = [row for row in self.list_summaries() if row.graph_id == graph.graph_id]
        if matches:
            if (
                len(matches) == 1
                and matches[0].scope is graph.scope
                and matches[0].graph == graph
            ):
                return graph
            raise AgentGraphStoreError(
                f"Workflow id {graph.graph_id} already exists with different content."
            )
        path = self.path_for(graph.scope, graph.graph_id)
        if path.exists():
            raise AgentGraphStoreError(
                f"Workflow id {graph.graph_id} already exists with different content."
            )
        self._write(path, graph)
        return graph

    def save(self, graph: WorkflowGraph) -> WorkflowGraph:
        """Overwrite an existing workflow in place, keeping its id and scope."""
        self._validate(graph)
        path = self.path_for(graph.scope, graph.graph_id)
        if not path.is_file():
            raise AgentGraphStoreError("That workflow no longer exists on disk.")
        self._write(path, graph)
        return graph

    def delete(self, scope: AgentScope, graph_id: str) -> bool:
        """Remove exactly the workflow at ``scope/id``."""
        self._require_graph_id(graph_id)
        # Discovery intentionally hides redirected storage. Deletion must be
        # stricter: returning "not found" through a linked scope would make a
        # refused delete look successful to its caller.
        for candidate_scope in (AgentScope.PROJECT, AgentScope.PERSONAL):
            self._require_safe_storage_path(
                self.directory(candidate_scope), action="delete"
            )
        row = self.summary_in_scope(scope, graph_id)
        if row is None or row.source is None:
            return False
        self._require_safe_storage_path(row.source, action="delete")
        try:
            row.source.unlink()
        except OSError as exc:
            raise AgentGraphStoreError(f"Could not delete that workflow: {exc}") from exc
        return True

    def _write(self, path: Path, graph: WorkflowGraph) -> None:
        try:
            self._require_safe_storage_path(path, action="write")
            path.parent.mkdir(parents=True, exist_ok=True)
            # mkdir may have raced with a link/junction insertion. Recheck the
            # complete chain immediately before the atomic replacement.
            self._require_safe_storage_path(path, action="write")
            atomic_write_bytes(path, render_graph_document(graph).encode("utf-8"))
        except AgentGraphStoreError:
            raise
        except OSError as exc:
            raise AgentGraphStoreError(f"Could not save that workflow: {exc}") from exc

    def _validate(self, graph: WorkflowGraph) -> None:
        self._require_graph_id(graph.graph_id)
        name_error = workflow_name_error(graph.name)
        if name_error:
            raise AgentGraphStoreError(name_error)
        self._require_allowed_references(graph)
        collision = any(
            row.graph_id == graph.graph_id and row.scope is not graph.scope
            for row in self.list_summaries()
        )
        if collision:
            raise AgentGraphStoreError(
                f"Another workflow already uses the id {graph.graph_id}."
            )

    def _require_allowed_references(self, graph: WorkflowGraph) -> None:
        """Refuse a save that would put an agent where its scope cannot go.

        An id this workspace cannot read is left alone: it stays in the file
        and shows on the canvas as a missing agent, because refusing to save
        would strand the user in a workflow they can no longer edit.
        """
        if self._agent_scopes is None:
            return
        try:
            known = dict(self._agent_scopes())
        except Exception:
            logger.debug("agents: could not resolve agent scopes", exc_info=True)
            return
        for agent_id in graph.agent_ids:
            scope = known.get(agent_id)
            if scope is None:
                continue
            error = reference_scope_error(graph.scope, scope)
            if error:
                raise AgentGraphStoreError(error[0].upper() + error[1:] + ".")

    @staticmethod
    def _require_graph_id(graph_id: object) -> str:
        if not is_valid_graph_id(graph_id):
            raise AgentGraphStoreError(
                f"'{graph_id}' is not a valid immutable workflow id."
            )
        return str(graph_id)

    @staticmethod
    def _require_safe_storage_path(path: Path, *, action: str) -> None:
        """Refuse every redirecting component from the volume root downward."""
        absolute = Path(path).absolute()
        anchor = Path(absolute.anchor)
        relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        linked = first_link_like_component(anchor, tuple(relative_parts))
        if linked is not None:
            raise AgentGraphStoreError(
                f"Could not {action} Agent workflows through a symlink, junction, "
                f"or redirecting reparse point ({linked})."
            )


def _duplicate_ids(ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for graph_id in ids:
        if graph_id in seen:
            duplicates.add(graph_id)
        seen.add(graph_id)
    return duplicates


def _reject_duplicate(row: WorkflowSummary, duplicates: set[str]) -> WorkflowSummary:
    """Refuse both sides of a duplicate id rather than picking a winner."""
    if row.graph_id not in duplicates:
        return row
    message = (
        f"the id {row.graph_id} is claimed by more than one workflow — ids are "
        "unique across project and personal workflows"
    )
    return WorkflowSummary(
        graph_id=row.graph_id,
        scope=row.scope,
        name=row.name,
        description=row.description,
        valid=False,
        errors=(*row.errors, message),
        graph=None,
        source=row.source,
    )


__all__ = [
    "NEW_WORKFLOW_NAME",
    "AgentGraphStore",
    "AgentGraphStoreError",
    "WorkflowSummary",
]
