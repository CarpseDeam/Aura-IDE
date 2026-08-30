"""The one shape a delegated run reports back in.

A child agent's conversation is private and ephemeral: its history, its
reasoning, and its tool transcript are discarded when it finishes.  The only
thing that survives is this structured result, paired to the root's own
``delegate_agent`` tool call and appended to canonical root History like any
other tool result.

The status is a fact about the run, never a verdict on the answer:

* ``completed`` — the child stopped because it had answered.
* ``partial``   — the child produced text but the run did not finish cleanly
  (the provider failed mid-run, or the stream ended without a response).
* ``failed``    — there is no answer: the agent could not be resolved, the
  provider or model could not be resolved, or the run produced nothing.
* ``cancelled`` — the user stopped the root turn while the child was running.

A child's provider or configuration failure is *its* failure and is reported
here, in the tool result.  It is never raised into the root conversation as a
harness error, and nothing here retries or silently substitutes another
provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DelegationStatus(str, Enum):
    """How a delegated run ended."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationFailure(str, Enum):
    """Why a delegation produced no usable answer.

    These name the *class* of failure so the root model can decide what to do
    next without parsing prose. They are stable identifiers, not messages.
    """

    #: The requested id is not on this turn's frozen roster.
    AGENT_NOT_AVAILABLE = "agent_not_available"
    #: The parent-authored task was missing or empty.
    TASK_MISSING = "task_missing"
    #: No workspace is open, so a child has nothing it may legitimately read.
    WORKSPACE_REQUIRED = "workspace_required"
    #: The definition names a provider this build does not know.
    PROVIDER_UNKNOWN = "provider_unknown"
    #: The provider is known but has no usable credentials on this machine.
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    #: The provider is real but cannot back an agent yet (CLI or local kinds).
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    #: Half a model target: a provider without a model, or the reverse.
    MODEL_TARGET_INCOMPLETE = "model_target_incomplete"
    #: The provider failed while the child was running.
    PROVIDER_ERROR = "provider_error"
    #: The child ran but produced no text at all.
    EMPTY_RESULT = "empty_result"
    #: A second delegation was requested while one was already running.
    DELEGATION_BUSY = "delegation_busy"
    #: Delegation is not available in this runtime at all (no child runner).
    DELEGATION_UNAVAILABLE = "delegation_unavailable"
    #: The child runtime raised where it should have reported.
    INTERNAL_ERROR = "internal_error"
    #: Writable execution requires a real, non-bare Git repository root.
    GIT_REPOSITORY_REQUIRED = "git_repository_required"
    #: Staged, unstaged, or untracked primary-worktree changes were present.
    PRIMARY_WORKTREE_DIRTY = "primary_worktree_dirty"
    #: Aura could not create the runtime-owned linked worktree.
    WORKTREE_CREATION_FAILED = "worktree_creation_failed"
    #: Stable child edits could not be committed for later inspection/application.
    CHECKPOINT_FAILED = "checkpoint_failed"


@dataclass(frozen=True)
class DelegationUsage:
    """What the child's provider reported it spent, when it reported anything."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        return not (
            self.prompt_tokens
            or self.completion_tokens
            or self.cache_hit_tokens
            or self.cache_miss_tokens
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


@dataclass(frozen=True)
class DelegationResult:
    """The complete, self-contained outcome of one delegated run."""

    status: DelegationStatus
    agent_id: str
    result: str = ""
    agent_name: str = ""
    failure_class: str = ""
    error: str = ""
    provider: str = ""
    model: str = ""
    usage: DelegationUsage | None = None
    #: Wall time the child actually ran for, or ``None`` when it never
    #: started. Zero is a real measurement and is reported as one.
    duration_ms: int | None = None
    #: Present for writable runs.  Empty values remain explicit in the payload
    #: so a failed writable invocation never looks like a read-only result.
    permission: str = ""
    change_set_id: str = ""
    base_sha: str = ""
    result_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    diffstat: str = ""
    tests_reported: tuple[dict[str, Any], ...] = ()
    final_report: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only for a run that finished because the child had answered."""
        return self.status is DelegationStatus.COMPLETED

    def payload(self) -> dict[str, Any]:
        """The tool-result body appended to canonical root History.

        Only keys that carry a fact are present: a completed run has no
        ``failure_class``, and a failed one makes no claim about an answer.
        """
        body: dict[str, Any] = {
            "ok": self.ok,
            "tool": "delegate_agent",
            "status": self.status.value,
            "agent_id": self.agent_id,
            "result": self.result,
        }
        if self.agent_name:
            body["agent_name"] = self.agent_name
        if self.failure_class:
            body["failure_class"] = self.failure_class
        if self.error:
            body["error"] = self.error
        if self.provider:
            body["provider"] = self.provider
        if self.model:
            body["model"] = self.model
        if self.usage is not None and not self.usage.is_empty:
            body["usage"] = self.usage.as_dict()
        if self.duration_ms is not None:
            body["duration_ms"] = self.duration_ms
        if self.permission and self.permission != "read_only":
            body.update(
                {
                    "permission": self.permission,
                    "change_set_id": self.change_set_id,
                    "base_sha": self.base_sha,
                    "result_sha": self.result_sha,
                    "changed_paths": list(self.changed_paths),
                    "diffstat": self.diffstat,
                    "tests_reported": list(self.tests_reported),
                    "final_report": self.final_report,
                }
            )
        for key, value in self.extras.items():
            body.setdefault(key, value)
        return body

    def to_json(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False)

    @classmethod
    def failure(
        cls,
        agent_id: str,
        failure: DelegationFailure,
        error: str,
        *,
        agent_name: str = "",
        provider: str = "",
        model: str = "",
    ) -> "DelegationResult":
        """A run that produced no answer, said plainly."""
        return cls(
            status=DelegationStatus.FAILED,
            agent_id=agent_id,
            agent_name=agent_name,
            failure_class=failure.value,
            error=error,
            provider=provider,
            model=model,
        )


__all__ = [
    "DelegationFailure",
    "DelegationResult",
    "DelegationStatus",
    "DelegationUsage",
]
