from __future__ import annotations
import ast
from .types import ProposalCapsule, CraftIssue, CraftIssueSeverity, CompiledPatch, CompilerBounce, CompilerReject, CraftDecision, filter_delta_issues
from .engine import CraftEngine
from .contract_gate import ContractGate
import dataclasses
from .reference_checker import ReferenceChecker
from .mutator import SafeMutator
from .formatter import CodeFormatter

class CompilerService:
    """The strict compiler boundary between LLM and user workspace.
    
    All LLM file writes must route through process_proposal().
    Tracks per-proposal retry state via a bounce counter.
    """
    
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._attempts: dict[str, int] = {}
        self._engine = CraftEngine()
        self._contract_gate = ContractGate()
        self._ref_checker = ReferenceChecker()
        self._mutator = SafeMutator()
        self._formatter = CodeFormatter()
    
    def process_proposal(self, capsule: ProposalCapsule, workspace_root=None) -> CompiledPatch | CompilerBounce | CompilerReject:
        """Main entry point. Returns CompiledPatch on success, CompilerBounce
        for repairable rejections, CompilerReject when max retries exhausted."""
        
        # Simple proposal ID for phase 1
        proposal_id = capsule.path.as_posix()
        
        attempt = self._attempts.get(proposal_id, 0) + 1
        self._attempts[proposal_id] = attempt
        
        decision = self._run_pipeline(capsule, workspace_root=workspace_root)
        
        if decision.approved:
            self.reset_attempts(proposal_id)
            return CompiledPatch(
                capsule=capsule,
                cleaned_code=decision.cleaned_code,
                checks_passed=[c for c in ["craft_engine", "contract_gate" if capsule.contract else None, "reference_checker"] if c is not None],
                checks_warned=list(decision.metadata.get("checks_warned", [])),
                metadata=dict(decision.metadata),
            )
        
        if attempt <= self.max_retries:
            repair_instructions = self._build_repair_instructions(decision.issues)
            return CompilerBounce(
                capsule=capsule,
                issues=decision.issues,
                repair_instructions=repair_instructions,
                attempt_number=attempt,
                max_attempts=self.max_retries,
                metadata=dict(decision.metadata),
            )
            
        return CompilerReject(
            capsule=capsule,
            issues=decision.issues,
            total_attempts=attempt,
            reason=f"Rejected after {attempt} attempts due to unresolvable issues.",
            metadata=dict(decision.metadata),
        )
    
    def _run_pipeline(self, capsule: ProposalCapsule, workspace_root=None):
        """Run the compiler pipeline stages. In Phase 1, delegates to CraftEngine."""
        
        metadata = {
            "syntax_valid": True,
            "pre_existing_environment_issues": [],
            "introduced_environment_issues": [],
            "quality_bounce": False,
            "failure_class": "",
            "write_outcome": "",
            "checks_warned": [],
            "craft_warnings": [],
        }

        # Stage 0: SafeMutator
        cleaned = self._mutator.mutate(capsule.proposed_code, path=capsule.path)
        if cleaned != capsule.proposed_code:
            capsule.proposed_code = cleaned
            try:
                capsule.ast_tree = ast.parse(cleaned)
            except SyntaxError:
                pass
        elif capsule.ast_tree is None:
            try:
                capsule.ast_tree = ast.parse(capsule.proposed_code)
            except SyntaxError:
                pass
                
        # Stage 1: Existing CraftEngine checks
        decision = self._engine.process_proposal(capsule)
        decision.metadata.update(metadata)
        decision = self._downgrade_soft_decision(decision)
        if any(issue.code == "syntax-error" for issue in decision.issues):
            decision.metadata.update(
                {
                    "syntax_valid": False,
                    "failure_class": "syntax_invalid",
                    "write_outcome": "not_applied_craft_rejected",
                }
            )
        elif not decision.approved and decision.issues:
            decision.metadata.update(
                {
                    "quality_bounce": True,
                    "failure_class": "quality_bounce",
                    "write_outcome": "not_applied_craft_rejected",
                }
            )
        
        # Stage 2: Contract Gate (runs for ALL files with a contract)
        if capsule.contract is not None:
            contract_issues = self._contract_gate.verify(capsule)
            if contract_issues:
                # Merge contract issues into the decision
                if decision.approved:
                    decision = CraftDecision(approved=False, issues=contract_issues, cleaned_code=capsule.proposed_code, metadata=dict(decision.metadata))
                else:
                    # Use a set to avoid duplicate issues, comparing by (code, line) for simplicity
                    existing_issues_set = {(issue.code, issue.line) for issue in decision.issues}
                    for new_issue in contract_issues:
                        if (new_issue.code, new_issue.line) not in existing_issues_set:
                            decision.issues.append(new_issue)
                            existing_issues_set.add((new_issue.code, new_issue.line))
                            
                decision.metadata.update(
                    {
                        "quality_bounce": True,
                        "failure_class": "quality_bounce",
                        "write_outcome": "not_applied_craft_rejected",
                    }
                )
                decision = self._downgrade_soft_decision(decision)

        # Stage 3: Reference Validation
        proposed_ref_issues = self._ref_checker.check(capsule, workspace_root=workspace_root)
        original_ref_issues = []
        if proposed_ref_issues:
            if not capsule.is_new_file and capsule.original_code:
                baseline_capsule = dataclasses.replace(
                    capsule,
                    proposed_code=capsule.original_code,
                    changed_line_ranges=[]
                )
                try:
                    baseline_capsule.ast_tree = ast.parse(capsule.original_code)
                except SyntaxError:
                    baseline_capsule.ast_tree = None
                
                original_ref_issues = self._ref_checker.check(baseline_capsule, workspace_root=workspace_root)

            filtered_ref_issues = filter_delta_issues(
                proposed_issues=proposed_ref_issues,
                original_issues=original_ref_issues,
                changed_ranges=capsule.changed_line_ranges,
                is_new_file=capsule.is_new_file
            )
            pre_existing_ref_issues = [
                issue for issue in proposed_ref_issues
                if issue not in filtered_ref_issues
            ]
            if pre_existing_ref_issues:
                decision.metadata["pre_existing_environment_issues"] = [
                    _issue_payload(issue) for issue in pre_existing_ref_issues
                ]
                decision.metadata["checks_warned"] = sorted(
                    set(decision.metadata.get("checks_warned", [])) | {"reference_checker"}
                )
            if filtered_ref_issues:
                hard_ref_issues = [issue for issue in filtered_ref_issues if issue.severity == CraftIssueSeverity.HARD]
                soft_ref_issues = [issue for issue in filtered_ref_issues if issue.severity == CraftIssueSeverity.SOFT]
                if soft_ref_issues:
                    _record_warnings(decision.metadata, soft_ref_issues, "reference_checker")
                if hard_ref_issues:
                    decision.metadata["introduced_environment_issues"] = [
                        _issue_payload(issue) for issue in hard_ref_issues
                    ]
                    decision.metadata["failure_class"] = "introduced_environment_issue"
                    decision.metadata["write_outcome"] = "not_applied_craft_rejected"

            # Merge filtered reference issues into the decision
            if filtered_ref_issues:
                hard_ref_issues = [issue for issue in filtered_ref_issues if issue.severity == CraftIssueSeverity.HARD]
                if decision.approved:
                    if hard_ref_issues:
                        decision = CraftDecision(
                            approved=False,
                            issues=hard_ref_issues,
                            cleaned_code=capsule.proposed_code,
                            metadata=dict(decision.metadata),
                        )
                elif hard_ref_issues:
                    existing_issues_set = {(issue.code, issue.line) for issue in decision.issues}
                    for new_issue in hard_ref_issues:
                        if (new_issue.code, new_issue.line) not in existing_issues_set:
                            decision.issues.append(new_issue)
                            existing_issues_set.add((new_issue.code, new_issue.line))
                decision = self._downgrade_soft_decision(decision)
                        
        # Final Stage: CodeFormatter
        if decision.approved:
            decision.cleaned_code = self._formatter.format_code(decision.cleaned_code, workspace_root=workspace_root)
            if decision.metadata.get("pre_existing_environment_issues"):
                decision.metadata["write_outcome"] = "applied_with_environment_caveat"
            else:
                decision.metadata["write_outcome"] = "applied"
        
        return decision

    def _downgrade_soft_decision(self, decision: CraftDecision) -> CraftDecision:
        soft_issues = [issue for issue in decision.issues if issue.severity == CraftIssueSeverity.SOFT]
        hard_issues = [issue for issue in decision.issues if issue.severity == CraftIssueSeverity.HARD]
        if soft_issues:
            _record_warnings(decision.metadata, soft_issues, "craft_engine")
        if decision.approved or hard_issues:
            decision.issues = hard_issues
            decision.approved = not hard_issues
            return decision

        decision.issues = []
        decision.approved = True
        decision.metadata["quality_bounce"] = False
        if decision.metadata.get("failure_class") == "quality_bounce":
            decision.metadata["failure_class"] = ""
        if decision.metadata.get("write_outcome") == "not_applied_craft_rejected":
            decision.metadata["write_outcome"] = ""
        return decision
    
    def _build_repair_instructions(self, issues: list[CraftIssue]) -> str:
        """Build human-readable repair instructions from issues list."""
        issues = [issue for issue in issues if issue.severity == CraftIssueSeverity.HARD]
        lines = ["Your code changes were rejected by the compiler. Please fix the following issues:"]
        for issue in issues:
            lines.append(f"- Line {issue.line}: [{issue.code}] {issue.message}")
            if issue.suggestion:
                lines.append(f"  Suggestion: {issue.suggestion}")
        return "\n".join(lines)
    
    def invalidate_workspace_index(self, workspace_root=None) -> None:
        """Clear cached workspace index in the reference checker."""
        self._ref_checker.invalidate_workspace_index(workspace_root)

    def reset_attempts(self, proposal_id: str) -> None:
        """Clear retry tracking for a proposal."""
        self._attempts.pop(proposal_id, None)


def _issue_payload(issue: CraftIssue) -> dict:
    severity = getattr(issue, "severity", "")
    return {
        "line": getattr(issue, "line", None),
        "column": getattr(issue, "column", None),
        "code": getattr(issue, "code", ""),
        "message": getattr(issue, "message", ""),
        "suggestion": getattr(issue, "suggestion", ""),
        "severity": getattr(severity, "value", str(severity)),
    }


def _record_warnings(metadata: dict, issues: list[CraftIssue], check_name: str) -> None:
    if not issues:
        return
    existing = {
        (
            item.get("code"),
            item.get("line"),
            item.get("column"),
            item.get("message"),
        )
        for item in metadata.get("craft_warnings", [])
        if isinstance(item, dict)
    }
    warnings = list(metadata.get("craft_warnings", []))
    for issue in issues:
        payload = _issue_payload(issue)
        key = (payload.get("code"), payload.get("line"), payload.get("column"), payload.get("message"))
        if key not in existing:
            warnings.append(payload)
            existing.add(key)
    metadata["craft_warnings"] = warnings
    metadata["checks_warned"] = sorted(set(metadata.get("checks_warned", [])) | {check_name})

# Module-level singleton
compiler_service = CompilerService()
