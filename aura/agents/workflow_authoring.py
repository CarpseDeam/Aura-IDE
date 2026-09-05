"""Qt-free conversational authoring through native stores and shared edits."""

from __future__ import annotations

from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.local_state import AgentLocalState
from aura.agents.retention import AgentTeamRetention
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.store import AgentStore
from aura.agents.team_spec import WorkflowSpec
from aura.agents.turn_context import AgentModelTargets
from aura.agents.workflow_builder import BuiltWorkflow, build_workflow
from aura.agents.workflow_document import WorkflowDocument, WorkflowSaved
from aura.agents.workflow_edits import WorkflowEdits


class WorkflowAuthoring:
    """One turn's authoring context; no runner or execution capability."""

    def __init__(
        self,
        *,
        agents: AgentStore,
        workflows: AgentGraphStore,
        local_state: AgentLocalState,
        edits: WorkflowEdits,
        model_targets: AgentModelTargets,
    ) -> None:
        self.agents = agents
        self.workflows = workflows
        self.local_state = local_state
        self.edits = edits
        self.model_targets = model_targets
        self._pending: dict[WorkflowSpec, BuiltWorkflow] = {}
        self._pending_updates: dict[tuple[str, str, WorkflowSpec], BuiltWorkflow] = {}
        self._retention = AgentTeamRetention(agents=agents, workflows=workflows, local_state=local_state)

    def roster(self) -> AgentTurnRoster:
        return AgentTurnRoster(
            tuple(
                AgentRosterEntry(row.definition, self.local_state.permission(row.agent_id))
                for row in self.agents.list_summaries()
                if row.valid and row.definition is not None
            )
        )

    def inspect(self, workflow_id: str = "") -> dict:
        """Discovery includes saved graphs which cannot currently execute."""
        if workflow_id:
            return self.document(workflow_id).payload()
        return {
            "workflows": [
                {"workflow_id": row.graph_id, "name": row.name, "scope": row.scope.value, "valid": row.valid}
                for row in self.workflows.list_summaries()
            ],
            "agents": self.roster().catalog_rows(),
            "model_targets": self.model_targets.catalog_rows(),
        }

    def document(self, workflow_id: str) -> WorkflowDocument:
        graph = self.workflows.get(workflow_id)
        if graph is None:
            raise AgentGraphStoreError("That saved Workflow is missing or cannot be loaded.")
        entries = tuple(entry for entry in self.roster().entries if entry.agent_id in graph.agent_ids)
        if {entry.agent_id for entry in entries} != set(graph.agent_ids):
            raise AgentGraphStoreError("This Workflow references a missing or invalid Agent.")
        return WorkflowDocument(graph, entries)

    def _checked(self, workflow_id: str, revision: str) -> WorkflowDocument:
        document = self.document(workflow_id)
        if not revision or document.revision != revision:
            raise AgentGraphStoreError(
                "This Workflow or one of its Agents changed. Inspect it again before editing or undoing."
            )
        return document

    def _build(self, spec: WorkflowSpec, base=None) -> BuiltWorkflow:
        built, errors = build_workflow(spec, roster=self.roster(), model_targets=self.model_targets, base=base)
        if built is None:
            raise AgentGraphStoreError("; ".join(errors))
        return built

    def create(self, spec: WorkflowSpec) -> WorkflowSaved:
        with self.edits.lock:
            built = self._pending.get(spec)
            if built is None:
                if any(row.name.casefold() == spec.name.casefold() for row in self.workflows.list_summaries()):
                    raise AgentGraphStoreError(
                        "A Workflow with this name already exists. Inspect and update it, or choose a distinct name."
                    )
                built = self._build(spec)
                self._pending[spec] = built
            # Retain the prepared identities on partial failure so a retry is exact.
            self._retention.save_workflow(built, self.workflows.create_supplied)
            return self._result(built.graph.graph_id, "Saved")

    def update(self, workflow_id: str, revision: str, spec: WorkflowSpec) -> WorkflowSaved:
        with self.edits.lock:
            before = self._checked(workflow_id, revision)
            key = (workflow_id, revision, spec)
            built = self._pending_updates.get(key)
            if built is None:
                built = self._build(spec, before.graph)
                self._pending_updates[key] = built

            def commit(graph):
                self._checked(workflow_id, revision)
                self.edits.commit(before.graph, graph)

            # Reused definitions and grants are checked, never rewritten.
            self._retention.save_workflow(built, commit)
            return self._result(workflow_id, "Updated")

    def undo(self, workflow_id: str, revision: str) -> WorkflowSaved:
        with self.edits.lock:
            before = self._checked(workflow_id, revision)
            if self.edits.step(before.graph) is None:
                raise AgentGraphStoreError("There is no Workflow edit to undo in this session.")
            return self._result(workflow_id, "Undone")

    def _result(self, workflow_id: str, status: str) -> WorkflowSaved:
        document = self.document(workflow_id)
        return WorkflowSaved(document, status, self.edits.history(document.graph).can_undo)
