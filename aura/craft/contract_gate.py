from __future__ import annotations
import ast
from aura.craft.types import ProposalCapsule, CraftIssue, ExplicitSpecContract, CraftIssueSeverity


class ContractGate:
    """Verifies a Worker's proposal against the Planner's ExplicitSpecContract.
    
    Runs for ALL Python files when a contract is present (unlike CraftEngine's
    authorship checks which only run for new Aura-owned files).
    """
    
    def verify(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Run all contract checks. Returns list of issues (empty = passed)."""
        issues: list[CraftIssue] = []
        if capsule.contract is None:
            return issues
        
        # Check expected public symbols exist
        if capsule.contract.expected_public_symbols:
            issues.extend(self._check_expected_symbols(capsule))
        
        # Check forbidden public methods aren't added
        if capsule.contract.forbidden_public_methods:
            issues.extend(self._check_forbidden_methods(capsule))
        
        # Check forbidden calls aren't used
        if capsule.contract.forbidden_calls:
            issues.extend(self._check_forbidden_calls(capsule))
        
        # Check dataclass fields
        if capsule.contract.expected_dataclass_fields:
            issues.extend(self._check_dataclass_fields(capsule))
        
        return issues
    
    def _check_expected_symbols(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Check that expected public symbols exist in the proposed code."""
        issues = []
        if not capsule.ast_tree or not capsule.contract:
            return issues

        defined_symbols = set()
        class_members: dict[str, set[str]] = {}
        for node in capsule.ast_tree.body:
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                defined_symbols.add(node.name)
            elif isinstance(node, ast.ClassDef):
                defined_symbols.add(node.name)
                class_members[node.name] = self._collect_class_members(node)
        
        for expected_symbol in capsule.contract.expected_public_symbols:
            if self._expected_symbol_exists(expected_symbol, defined_symbols, class_members):
                continue

            issues.append(CraftIssue(
                line=1, column=0,
                code="CONTRACT_MISSING_SYMBOL",
                message=f"Contract requires public symbol '{expected_symbol}' but it was not found.",
                suggestion="Implement the required public symbol.",
                severity=CraftIssueSeverity.HARD
            ))
        return issues

    @staticmethod
    def _collect_class_members(node: ast.ClassDef) -> set[str]:
        members: set[str] = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(item.name)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                members.add(item.target.id)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        members.add(target.id)
        return members

    @staticmethod
    def _expected_symbol_exists(
        expected_symbol: str,
        defined_symbols: set[str],
        class_members: dict[str, set[str]],
    ) -> bool:
        if expected_symbol in defined_symbols:
            return True

        if "." not in expected_symbol:
            return False

        class_name, member_name = expected_symbol.split(".", 1)
        if not class_name or not member_name:
            return False
        return member_name in class_members.get(class_name, set())
    
    def _check_forbidden_methods(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Check that no forbidden public methods were added."""
        issues = []
        if not capsule.ast_tree or not capsule.contract:
            return issues

        for node in capsule.ast_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in capsule.contract.forbidden_public_methods:
                    issues.append(CraftIssue(
                        line=node.lineno, column=node.col_offset,
                        code="CONTRACT_FORBIDDEN_METHOD",
                        message=f"Forbidden public method '{node.name}' was added.",
                        suggestion="Remove or rename the forbidden method.",
                        severity=CraftIssueSeverity.HARD
                    ))
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            method_name = f"{node.name}.{item.name}"
                            if method_name in capsule.contract.forbidden_public_methods:
                                issues.append(CraftIssue(
                                    line=item.lineno, column=item.col_offset,
                                    code="CONTRACT_FORBIDDEN_METHOD",
                                    message=f"Forbidden public method '{method_name}' was added.",
                                    suggestion="Remove or rename the forbidden method.",
                                    severity=CraftIssueSeverity.HARD
                                ))
        return issues
    
    def _check_forbidden_calls(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Check that forbidden API calls are not used."""
        issues = []
        if not capsule.contract:
            return issues

        for forbidden_call in capsule.contract.forbidden_calls:
            if forbidden_call in capsule.proposed_code:
                # This is a very basic check. A more robust solution would use AST.
                # For now, a simple string match is sufficient for Phase 2.
                issues.append(CraftIssue(
                    line=1, column=0, # Line number is hard to get with simple string match
                    code="CONTRACT_FORBIDDEN_CALL",
                    message=f"Forbidden call '{forbidden_call}' detected in proposed code.",
                    suggestion=f"Remove all usages of '{forbidden_call}'.",
                    severity=CraftIssueSeverity.HARD
                ))
        return issues
    
    def _check_dataclass_fields(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Check expected dataclass fields exist."""
        issues = []
        if not capsule.ast_tree or not capsule.contract:
            return issues

        for node in capsule.ast_tree.body:
            if isinstance(node, ast.ClassDef):
                if any(decorator.id == 'dataclass' for decorator in node.decorator_list if isinstance(decorator, ast.Name)):
                    class_name = node.name
                    if class_name in capsule.contract.expected_dataclass_fields:
                        expected_fields = set(capsule.contract.expected_dataclass_fields[class_name])
                        actual_fields = set()
                        for item in node.body:
                            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                                actual_fields.add(item.target.id)
                            elif isinstance(item, ast.Expr) and isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Name) and item.value.func.id == 'field':
                                # This is a bit of a hack, but tries to catch field() definitions.
                                # A more robust solution would need to resolve the decorator call.
                                if isinstance(item.value.args[0], ast.Name):
                                     actual_fields.add(item.value.args[0].id)

                        missing_fields = expected_fields - actual_fields
                        for missing_field in missing_fields:
                            issues.append(CraftIssue(
                                line=node.lineno, column=node.col_offset,
                                code="CONTRACT_MISSING_DATACLASS_FIELD",
                                message=f"Dataclass '{class_name}' is missing expected field '{missing_field}'.",
                                suggestion=f"Add field '{missing_field}' to dataclass '{class_name}'.",
                                severity=CraftIssueSeverity.HARD
                            ))
        return issues
