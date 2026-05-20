from __future__ import annotations
from .types import ProposalCapsule, CraftIssue, CompiledPatch, CompilerBounce, CompilerReject
from .engine import CraftEngine


class CompilerService:
    """The strict compiler boundary between LLM and user workspace.
    
    All LLM file writes must route through process_proposal().
    Tracks per-proposal retry state via a bounce counter.
    """
    
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._attempts: dict[str, int] = {}  # proposal_id -> attempt count
        self._engine = CraftEngine()
    
    def process_proposal(self, capsule: ProposalCapsule) -> CompiledPatch | CompilerBounce | CompilerReject:
        """Main entry point. Returns CompiledPatch on success, CompilerBounce
        for repairable rejections, CompilerReject when max retries exhausted."""
        
        # Simple proposal ID for phase 1
        proposal_id = capsule.path.as_posix()
        
        attempt = self._attempts.get(proposal_id, 0) + 1
        self._attempts[proposal_id] = attempt
        
        decision = self._run_pipeline(capsule)
        
        if decision.approved:
            self.reset_attempts(proposal_id)
            return CompiledPatch(
                capsule=capsule,
                cleaned_code=decision.cleaned_code,
                checks_passed=["craft_engine"],
            )
        
        if attempt <= self.max_retries:
            repair_instructions = self._build_repair_instructions(decision.issues)
            return CompilerBounce(
                capsule=capsule,
                issues=decision.issues,
                repair_instructions=repair_instructions,
                attempt_number=attempt,
                max_attempts=self.max_retries,
            )
            
        return CompilerReject(
            capsule=capsule,
            issues=decision.issues,
            total_attempts=attempt,
            reason=f"Rejected after {attempt} attempts due to unresolvable issues.",
        )
    
    def _run_pipeline(self, capsule: ProposalCapsule):
        """Run the compiler pipeline stages. In Phase 1, delegates to CraftEngine."""
        return self._engine.process_proposal(capsule)
    
    def _build_repair_instructions(self, issues: list[CraftIssue]) -> str:
        """Build human-readable repair instructions from issues list."""
        lines = ["Your code changes were rejected by the compiler. Please fix the following issues:"]
        for issue in issues:
            lines.append(f"- Line {issue.line}: [{issue.code}] {issue.message}")
            if issue.suggestion:
                lines.append(f"  Suggestion: {issue.suggestion}")
        return "\n".join(lines)
    
    def reset_attempts(self, proposal_id: str) -> None:
        """Clear retry tracking for a proposal."""
        self._attempts.pop(proposal_id, None)

# Module-level singleton
compiler_service = CompilerService()
