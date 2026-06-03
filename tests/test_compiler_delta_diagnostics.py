import pytest
from pathlib import Path
from aura.craft.compiler import CompilerService
from aura.craft.types import ProposalCapsule, CraftIssue, CraftIssueSeverity, ExplicitSpecContract
from aura.craft.reference_checker import ReferenceChecker

class MockReferenceChecker(ReferenceChecker):
    def __init__(self):
        self.mock_issues = {}

    def check(self, capsule: ProposalCapsule, workspace_root=None) -> list[CraftIssue]:
        return self.mock_issues.get(capsule.proposed_code, [])

@pytest.fixture
def compiler():
    svc = CompilerService()
    svc._ref_checker = MockReferenceChecker()
    return svc

def test_pre_existing_issues_filtered_outside_changed_ranges(compiler):
    original_code = "def foo():\n    return undefined_var\n"
    proposed_code = "def foo():\n    return undefined_var\n\ndef bar():\n    return True\n"
    
    # Issue is on line 2, which is outside the changed range of lines 4-5
    issue = CraftIssue(line=2, column=11, code="undefined-name", message="Undefined name 'undefined_var'", suggestion="")
    compiler._ref_checker.mock_issues[original_code] = [issue]
    compiler._ref_checker.mock_issues[proposed_code] = [issue]
    
    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(4, 6)],
        is_new_file=False,
        ast_tree=ast.parse(proposed_code)
    )
    
    result = compiler.process_proposal(cap)
    assert result.__class__.__name__ == "CompiledPatch", f"Expected CompiledPatch, got {result.__class__.__name__} with issues: {getattr(result, 'issues', [])}"

def test_pre_existing_issue_near_changes_warns(compiler):
    original_code = "def foo():\n    return undefined_var\n"
    proposed_code = "def foo():\n    print('added')\n    return undefined_var\n"
    
    issue_orig = CraftIssue(line=2, column=11, code="undefined-name", message="Undefined name 'undefined_var'", suggestion="")
    issue_prop = CraftIssue(line=3, column=11, code="undefined-name", message="Undefined name 'undefined_var'", suggestion="")
    
    compiler._ref_checker.mock_issues[original_code] = [issue_orig]
    compiler._ref_checker.mock_issues[proposed_code] = [issue_prop]
    
    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(2, 3)], # line 2 changed (added print)
        is_new_file=False,
        ast_tree=ast.parse(proposed_code)
    )
    
    result = compiler.process_proposal(cap)
    assert result.__class__.__name__ == "CompiledPatch"
    assert result.metadata["pre_existing_environment_issues"][0]["code"] == "undefined-name"
    assert "reference_checker" in result.checks_warned

def test_new_issues_block(compiler):
    original_code = "def foo():\n    return True\n"
    proposed_code = "def foo():\n    return True\n\ndef bar():\n    return undefined_var\n"
    
    issue = CraftIssue(line=5, column=11, code="undefined-name", message="Undefined name 'undefined_var'", suggestion="")
    compiler._ref_checker.mock_issues[original_code] = []
    compiler._ref_checker.mock_issues[proposed_code] = [issue]
    
    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(4, 6)],
        is_new_file=False,
        ast_tree=ast.parse(proposed_code)
    )
    
    result = compiler.process_proposal(cap)
    assert result.__class__.__name__ == "CompilerBounce"
    assert len(result.issues) == 1
    assert result.issues[0].code == "undefined-name"

def test_soft_issues_warn_without_compiler_bounce(compiler):
    original_code = "def foo():\n    return True\n"
    proposed_code = "def foo():\n    return True\n\ndef bar():\n    return True\n"

    issue = CraftIssue(
        line=4,
        column=0,
        code="call-signature",
        message="Low-confidence signature concern.",
        suggestion="Review the call.",
        severity=CraftIssueSeverity.SOFT,
    )
    compiler._ref_checker.mock_issues[original_code] = []
    compiler._ref_checker.mock_issues[proposed_code] = [issue]

    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(4, 6)],
        is_new_file=False,
        ast_tree=ast.parse(proposed_code)
    )

    result = compiler.process_proposal(cap)
    assert result.__class__.__name__ == "CompiledPatch"
    assert "reference_checker" in result.checks_warned
    assert result.metadata["craft_warnings"][0]["code"] == "call-signature"

def test_new_files_block_on_all(compiler):
    proposed_code = "def foo():\n    return undefined_var\n"
    
    issue = CraftIssue(line=2, column=11, code="undefined-name", message="Undefined name 'undefined_var'", suggestion="")
    compiler._ref_checker.mock_issues[proposed_code] = [issue]
    
    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code="",
        proposed_code=proposed_code,
        changed_line_ranges=[(1, 3)],
        is_new_file=True,
        ast_tree=ast.parse(proposed_code)
    )
    
    result = compiler.process_proposal(cap)
    assert result.__class__.__name__ == "CompilerBounce"
    assert len(result.issues) == 1

def test_syntax_errors_still_block():
    original_code = "def foo():\n    return True\n"
    proposed_code = "def foo():\n    return True\ndef bar()\n    print('missing colon')\n"
    
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(3, 5)],
        is_new_file=False
    )
    
    svc = CompilerService()
    # We do not mock anything here because syntax error is handled by the CraftEngine
    result = svc.process_proposal(cap)
    assert result.__class__.__name__ == "CompilerBounce"
    assert any(i.code == "syntax-error" for i in result.issues)

def test_contract_gate_issues_still_block():
    original_code = "def foo():\n    return True\n"
    proposed_code = "def foo():\n    return True\ndef bar():\n    return True\n"
    
    contract = ExplicitSpecContract(forbidden_public_methods=["bar"])
    
    import ast
    cap = ProposalCapsule(
        path=Path("test.py"),
        language="python",
        tool_name="test",
        original_code=original_code,
        proposed_code=proposed_code,
        changed_line_ranges=[(3, 5)],
        is_new_file=False,
        contract=contract,
        ast_tree=ast.parse(proposed_code)
    )
    
    svc = CompilerService()
    result = svc.process_proposal(cap)
    assert result.__class__.__name__ == "CompilerBounce"
    assert any(i.code.startswith("CONTRACT_") for i in result.issues)
