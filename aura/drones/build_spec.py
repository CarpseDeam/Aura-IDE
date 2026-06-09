from __future__ import annotations

from dataclasses import dataclass

KIND_PROJECT_WORKER = "project_worker"
KIND_BROWSER_WATCHER = "browser_watcher"
KIND_EMAIL_WATCHER = "email_watcher"
KIND_DASHBOARD_SUMMARIZER = "dashboard_summarizer"
KIND_MARKET_WATCHER = "market_watcher"
KIND_REPO_WATCHER = "repo_watcher"
KIND_REPORT_DRAFTER = "report_drafter"
KIND_CUSTOM_CHORE = "custom_chore"

SUPPORTED_SPEC_KINDS = (
    KIND_PROJECT_WORKER,
    KIND_BROWSER_WATCHER,
    KIND_EMAIL_WATCHER,
    KIND_DASHBOARD_SUMMARIZER,
    KIND_MARKET_WATCHER,
    KIND_REPO_WATCHER,
    KIND_REPORT_DRAFTER,
    KIND_CUSTOM_CHORE,
)

STATUS_BUILDABLE_NOW = "buildable_now"
STATUS_NEEDS_CAPABILITY = "needs_capability"
STATUS_NEEDS_MORE_INFO = "needs_more_info"

VALID_BUILD_STATUSES = (STATUS_BUILDABLE_NOW, STATUS_NEEDS_CAPABILITY, STATUS_NEEDS_MORE_INFO)

VALID_WRITE_POLICIES = ("read_only", "ask_before_writes", "normal_diff_approval")


@dataclass(frozen=True)
class DroneBuildSpec:
    """Blueprint for building a new Drone."""

    name: str = ""
    kind: str = ""
    job: str = ""
    trigger: str = ""
    required_access: tuple[str, ...] = ()
    write_policy: str = ""
    action_policy: str = ""
    capabilities_needed: tuple[str, ...] = ()
    instructions: str = ""
    output_contract: str = ""
    success_criteria: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    build_status: str = ""
    missing_capabilities: tuple[str, ...] = ()
    first_run_test: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "job": self.job,
            "trigger": self.trigger,
            "required_access": self.required_access,
            "write_policy": self.write_policy,
            "action_policy": self.action_policy,
            "capabilities_needed": self.capabilities_needed,
            "instructions": self.instructions,
            "output_contract": self.output_contract,
            "success_criteria": self.success_criteria,
            "boundaries": self.boundaries,
            "assumptions": self.assumptions,
            "build_status": self.build_status,
            "missing_capabilities": self.missing_capabilities,
            "first_run_test": self.first_run_test,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> DroneBuildSpec:
        """Build a spec from a dict, forgivingly coercing types.

        Missing string keys → ``""``, missing list keys → ``()``.
        Single strings are coerced to one-item tuples for tuple fields.
        Unknown keys are silently ignored.
        """

        def _str(key: str) -> str:
            val = data.get(key)
            return val if isinstance(val, str) else ""

        def _tuple_str(key: str) -> tuple[str, ...]:
            val = data.get(key)
            if isinstance(val, str):
                return (val,)
            if isinstance(val, (list, tuple)):
                return tuple(str(v) for v in val)
            return ()

        return DroneBuildSpec(
            name=_str("name"),
            kind=_str("kind"),
            job=_str("job"),
            trigger=_str("trigger"),
            required_access=_tuple_str("required_access"),
            write_policy=_str("write_policy"),
            action_policy=_str("action_policy"),
            capabilities_needed=_tuple_str("capabilities_needed"),
            instructions=_str("instructions"),
            output_contract=_str("output_contract"),
            success_criteria=_tuple_str("success_criteria"),
            boundaries=_tuple_str("boundaries"),
            assumptions=_tuple_str("assumptions"),
            build_status=_str("build_status"),
            missing_capabilities=_tuple_str("missing_capabilities"),
            first_run_test=_str("first_run_test"),
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable error strings (never raises)."""
        errors: list[str] = []

        # Rule 1 — required non-empty string fields
        for field_name in (
            "name",
            "kind",
            "job",
            "write_policy",
            "instructions",
            "output_contract",
            "build_status",
        ):
            val = getattr(self, field_name, "")
            if not isinstance(val, str) or val.strip() == "":
                errors.append(f"{field_name} must be a non-empty string")

        # Rule 2 — kind must be supported (only when non-empty)
        if self.kind and self.kind not in SUPPORTED_SPEC_KINDS:
            supported = ", ".join(SUPPORTED_SPEC_KINDS)
            errors.append(f"unsupported kind '{self.kind}' (must be one of {supported})")

        # Rule 3 — build_status must be valid (only when non-empty)
        if self.build_status and self.build_status not in VALID_BUILD_STATUSES:
            supported = ", ".join(VALID_BUILD_STATUSES)
            errors.append(
                f"unsupported build_status '{self.build_status}' (must be one of {supported})"
            )

        # Rule 4 — write_policy must be valid (only when non-empty)
        if self.write_policy and self.write_policy not in VALID_WRITE_POLICIES:
            supported = ", ".join(VALID_WRITE_POLICIES)
            errors.append(
                f"unsupported write_policy '{self.write_policy}' (must be one of {supported})"
            )

        # Rule 5 — needs_capability requires non-empty missing_capabilities
        if self.build_status == STATUS_NEEDS_CAPABILITY and not self.missing_capabilities:
            errors.append(
                "build_status is 'needs_capability' but missing_capabilities is empty"
            )

        # Rule 6 — buildable_now requires non-empty instructions and output_contract
        if self.build_status == STATUS_BUILDABLE_NOW:
            if not self.instructions.strip():
                errors.append("build_status is 'buildable_now' but instructions is empty")
            if not self.output_contract.strip():
                errors.append(
                    "build_status is 'buildable_now' but output_contract is empty"
                )

        return errors

    def is_buildable_now(self) -> bool:
        return self.build_status == STATUS_BUILDABLE_NOW and len(self.validate()) == 0

    def preview_markdown(self) -> str:
        """Return a Markdown string for the Workshop preview panel."""
        lines: list[str] = []

        title = self.name if self.name else "Untitled Drone"
        lines.append(f"# {title}")

        lines.append(f"**Kind**: {self.kind}")
        lines.append(f"**Job**: {self.job}")

        # Build status with contextual note
        status_note = ""
        if self.build_status == STATUS_BUILDABLE_NOW:
            status_note = "✅ Ready to build"
        elif self.build_status == STATUS_NEEDS_CAPABILITY:
            status_note = "⚠️ Needs new capability"
        elif self.build_status == STATUS_NEEDS_MORE_INFO:
            status_note = "❓ Needs more information"
        if status_note:
            lines.append(f"**Build Status**: {self.build_status} — {status_note}")
        else:
            lines.append(f"**Build Status**: {self.build_status}")

        lines.append(f"**Permissions**: {self.write_policy}")

        if self.action_policy:
            lines.append(f"**Action Policy**: {self.action_policy}")
        if self.trigger:
            lines.append(f"**Trigger**: {self.trigger}")

        if self.required_access:
            lines.append("**Required Access**:")
            for item in self.required_access:
                lines.append(f"- {item}")

        lines.append(f"**What it will do**: {self.instructions}")
        lines.append(f"**Output**: {self.output_contract}")

        if self.success_criteria:
            lines.append("**Success Criteria**:")
            for i, item in enumerate(self.success_criteria, 1):
                lines.append(f"{i}. {item}")

        if self.boundaries:
            lines.append("**Boundaries**:")
            for item in self.boundaries:
                lines.append(f"- {item}")

        if self.assumptions:
            lines.append("**Assumptions**:")
            for item in self.assumptions:
                lines.append(f"- {item}")

        if self.missing_capabilities:
            lines.append("**Missing Capabilities**:")
            for item in self.missing_capabilities:
                lines.append(f"- {item}")

        if self.first_run_test:
            lines.append(f"**First Run Test**: {self.first_run_test}")

        return "\n".join(lines)
