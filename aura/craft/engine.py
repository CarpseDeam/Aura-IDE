import ast
import logging
import re
from .types import CraftDecision, CraftIssue, CraftIssueSeverity, ProposalCapsule, node_in_ranges, line_in_ranges
from aura.quality.features import _GENERIC_NAMES

_log = logging.getLogger(__name__)

def _is_narration_comment(line_text: str) -> bool:
    stripped = line_text.strip()
    if not stripped.startswith("#"):
        return False
    text = stripped[1:].strip().lower()
    prefixes = [
        "initialize", "process", "loop through", "iterate through", 
        "create", "check if", "this function", "this method"
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            return True
    return False

def _is_private_helper_docstring_line(line_text: str) -> bool:
    stripped = line_text.strip()
    if not (stripped.startswith('"""') and stripped.endswith('"""')):
        return False
    if len(stripped) < 6:
        return False
    inner = stripped[3:-3].strip().lower()
    targets = [
        "helper", "internal helper", "private helper",
        "utility function", "utility method", "small helper"
    ]
    return inner in targets

def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return body with leading docstring expression removed, if present."""
    if not body:
        return body
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _generic_name_count(tree: ast.AST, generic_set: set[str]) -> int:
    """Count distinct uses of generic names as assignment targets or function params."""
    seen: set[tuple[str, int]] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if arg.arg in generic_set:
                    seen.add((arg.arg, arg.lineno))
            if node.args.vararg and node.args.vararg.arg in generic_set:
                seen.add((node.args.vararg.arg, node.args.vararg.lineno))
            if node.args.kwarg and node.args.kwarg.arg in generic_set:
                seen.add((node.args.kwarg.arg, node.args.kwarg.lineno))

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_name(target, generic_set, seen)
        elif isinstance(node, ast.NamedExpr):
            _collect_name(node.target, generic_set, seen)

    return len(seen)


def _collect_name(node: ast.AST, generic_set: set[str], seen: set) -> None:
    """Collect assignment target names that match generic_set."""
    if isinstance(node, ast.Name):
        if node.id in generic_set:
            key = (node.id, node.lineno)
            if key not in seen:
                seen.add(key)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _collect_name(elt, generic_set, seen)
    elif isinstance(node, ast.Starred):
        _collect_name(node.value, generic_set, seen)


class CraftEngine:
    def process_proposal(self, capsule: ProposalCapsule) -> CraftDecision:
        if capsule.language != "python" or not str(capsule.path).endswith(".py"):
            return CraftDecision(approved=True, cleaned_code=capsule.proposed_code)
            
        # Phase A: Cleanup
        cleaned_code = capsule.proposed_code
        try:
            # Strip markdown fences
            if cleaned_code.startswith("```python\n") and cleaned_code.endswith("\n```"):
                cleaned_code = cleaned_code[10:-4]
            elif cleaned_code.startswith("```python\r\n") and cleaned_code.endswith("\r\n```"):
                cleaned_code = cleaned_code[11:-5]
                
            lines = cleaned_code.splitlines()
            new_lines = []
            
            for i, line in enumerate(lines):
                line_num = i + 1
                should_clean = True
                if not capsule.is_new_file and capsule.changed_line_ranges:
                    if not line_in_ranges(line_num, capsule.changed_line_ranges):
                        should_clean = False
                        
                if should_clean:
                    if _is_narration_comment(line):
                        continue
                    if _is_private_helper_docstring_line(line):
                        continue
                new_lines.append(line)
                
            temp_code = "\n".join(new_lines) + ("\n" if cleaned_code.endswith("\n") else "")
            
            # Verify parses
            ast.parse(temp_code)
            cleaned_code = temp_code
            
        except SyntaxError as e:
            # Fall back to raw
            pass
        except Exception as e:
            _log.warning("CraftEngine Phase A failed: %s", e)
            
        # Phase B: Blockers
        issues = []
        try:
            tree = ast.parse(cleaned_code)
        except SyntaxError as e:
            issues.append(CraftIssue(
                line=e.lineno or 0,
                column=e.offset or 0,
                code="syntax-error",
                message=f"Syntax error: {e.msg}",
                suggestion="Fix the syntax error."
            ))
            return CraftDecision(approved=False, cleaned_code=cleaned_code, issues=issues)
            
        is_test_file = "/test" in str(capsule.path).replace("\\", "/") or "test" in capsule.path.stem.lower()

        for node in ast.walk(tree):
            if not capsule.is_new_file and capsule.changed_line_ranges:
                if not node_in_ranges(node, capsule.changed_line_ranges):
                    continue
                    
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # B2: Stub body
                if len(node.body) == 1:
                    stmt = node.body[0]
                    is_stub = False
                    if isinstance(stmt, ast.Pass):
                        is_stub = True
                    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                        is_stub = True
                    elif isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Name) and stmt.exc.id == "NotImplementedError":
                        is_stub = True
                        
                    if is_stub:
                        issues.append(CraftIssue(
                            line=node.lineno,
                            column=node.col_offset,
                            code="stub-body-pass",
                            message=f"Function '{node.name}' has a stub body.",
                            suggestion="Implement the function fully. Do not leave placeholders."
                        ))
                        
                # B4: Scaffolding keywords
                if not is_test_file:
                    name_lower = node.name.lower()
                    if any(kw in name_lower for kw in ["demo", "placeholder", "dummy", "mockwindow", "mockwidget"]):
                        issues.append(CraftIssue(
                            line=node.lineno,
                            column=node.col_offset,
                            code="demo-scaffolding",
                            message=f"Function '{node.name}' appears to be demo or mock scaffolding.",
                            suggestion="Do not include demo or placeholder functions in production code."
                        ))

            elif isinstance(node, ast.ClassDef):
                if not is_test_file:
                    name_lower = node.name.lower()
                    if any(kw in name_lower for kw in ["demo", "placeholder", "dummy", "mockwindow", "mockwidget"]):
                        issues.append(CraftIssue(
                            line=node.lineno,
                            column=node.col_offset,
                            code="demo-scaffolding",
                            message=f"Class '{node.name}' appears to be demo or mock scaffolding.",
                            suggestion="Do not include demo or placeholder classes in production code."
                        ))

            elif isinstance(node, ast.ExceptHandler):
                # B3: Silent exception swallowing
                is_bare = node.type is None
                
                is_swallowed = False
                swallow_code = ""
                
                if is_bare:
                    is_swallowed = True
                    swallow_code = "bare-except"
                else:
                    # check if except Exception
                    if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                        if len(node.body) == 1:
                            if isinstance(node.body[0], ast.Pass):
                                is_swallowed = True
                                swallow_code = "swallow-except-pass"
                            elif isinstance(node.body[0], ast.Return):
                                if isinstance(node.body[0].value, ast.Constant) and node.body[0].value.value is None:
                                    is_swallowed = True
                                    swallow_code = "swallow-except-return-none"
                                elif node.body[0].value is None: # bare return
                                    is_swallowed = True
                                    swallow_code = "swallow-except-return-none"
                                    
                if is_swallowed:
                    issues.append(CraftIssue(
                        line=node.lineno,
                        column=node.col_offset,
                        code=swallow_code or "bare-except",
                        message="Exception handler silently swallows exceptions.",
                        suggestion="Handle the exception properly, log it, or raise it. Do not swallow exceptions silently."
                    ))

        if issues:
            return CraftDecision(approved=False, cleaned_code=cleaned_code, issues=issues)

        # Phase C: Authorship soft checks for new Aura-owned Python files
        if capsule.is_new_file and str(capsule.path).startswith("aura/"):
            authorship_issues = self._run_authorship_checks(capsule)
            if authorship_issues:
                return CraftDecision(approved=False, cleaned_code=cleaned_code, issues=authorship_issues)
            
        return CraftDecision(approved=True, cleaned_code=cleaned_code)

    def _run_authorship_checks(self, capsule: ProposalCapsule) -> list[CraftIssue]:
        """Run all 7 soft authorship checks for new Aura-owned Python files."""
        try:
            tree = ast.parse(capsule.proposed_code)
        except SyntaxError:
            return []

        issues: list[CraftIssue] = []
        source_lines = capsule.proposed_code.splitlines()

        # noop_init
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        body = _strip_docstring(item.body)
                        if not body or (len(body) == 1 and isinstance(body[0], ast.Pass)):
                            issues.append(CraftIssue(
                                line=item.lineno,
                                column=item.col_offset,
                                code="noop_init",
                                message=f"Constructor '{item.name}' has an empty body.",
                                suggestion="Remove unnecessary __init__ or add initialization logic.",
                                severity=CraftIssueSeverity.SOFT,
                            ))

        # section_banner
        for lineno, line_text in enumerate(source_lines, start=1):
            stripped = line_text.strip()
            if not stripped.startswith("#"):
                continue
            if re.match(r'^#[=*/\\-]{3,}\s*\w+.*[=*/\\-]{3,}$', stripped) or \
               re.match(r'^#\s*-{3,}\s', stripped) or \
               re.match(r'^#\s*={3,}\s', stripped) or \
               re.match(r'^#[=*\-~]{4,}$', stripped):
                issues.append(CraftIssue(
                    line=lineno,
                    column=0,
                    code="section_banner",
                    message="Decorative section banner comment.",
                    suggestion="Remove decorative section banners. Use focused comments instead.",
                    severity=CraftIssueSeverity.SOFT,
                ))

        # boilerplate_docstring
        # Module-level docstring
        if (tree.body
                and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            doc = tree.body[0].value.value.strip().lower()
            if doc.startswith(("module ", "this module ", "this file ")):
                issues.append(CraftIssue(
                    line=tree.body[0].lineno,
                    column=tree.body[0].col_offset,
                    code="boilerplate_docstring",
                    message="Module-level docstring is boilerplate.",
                    suggestion="Remove or replace with a specific, useful description.",
                    severity=CraftIssueSeverity.SOFT,
                ))

        # Function/method docstrings
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    doc = node.body[0].value.value.strip().lower()
                    if doc.startswith(("this function ", "this method ", "this class ")):
                        issues.append(CraftIssue(
                            line=node.body[0].lineno,
                            column=node.body[0].col_offset,
                            code="boilerplate_docstring",
                            message=f"Docstring for '{node.name}' is boilerplate.",
                            suggestion="Replace with a specific description or remove it.",
                            severity=CraftIssueSeverity.SOFT,
                        ))

        # staticmethod_class
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [item for item in node.body if isinstance(item, ast.FunctionDef)]
                if not methods:
                    continue
                has_init = any(m.name == "__init__" for m in methods)
                if has_init:
                    continue
                all_static = True
                for m in methods:
                    is_static = any(
                        (isinstance(d, ast.Name) and d.id == "staticmethod")
                        or (isinstance(d, ast.Attribute) and d.attr == "staticmethod")
                        for d in m.decorator_list
                    )
                    if not is_static:
                        all_static = False
                        break
                if all_static:
                    issues.append(CraftIssue(
                        line=node.lineno,
                        column=node.col_offset,
                        code="staticmethod_class",
                        message=f"Class '{node.name}' contains only static methods.",
                        suggestion="Use module-level functions instead of a static-method-only class.",
                        severity=CraftIssueSeverity.SOFT,
                    ))

        # clever_helper
        # Count call sites per function name
        call_counts: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                call_counts[node.func.id] = call_counts.get(node.func.id, 0) + 1

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("_"):
                continue
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            body = _strip_docstring(node.body)
            logic_stmts = [s for s in body if not isinstance(s, (ast.Pass,))]
            if len(logic_stmts) > 3:
                continue
            call_count = call_counts.get(node.name, 0)
            if call_count != 1:
                continue
            issues.append(CraftIssue(
                line=node.lineno,
                column=node.col_offset,
                code="clever_helper",
                message=f"Private function '{node.name}' has {len(logic_stmts)} logic line(s) and one call site.",
                suggestion="Inline the helper at its call site or remove it.",
                severity=CraftIssueSeverity.SOFT,
            ))

        # silent_except_return
        _LOGGING_ATTRS = {"warning", "error", "exception", "critical", "info"}

        def _has_logging_call(body: list[ast.stmt]) -> bool:
            for stmt in body:
                for sub in ast.walk(stmt):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr in _LOGGING_ATTRS):
                        return True
            return False

        def _is_default_value(val: ast.expr | None) -> bool:
            if val is None:
                return True
            if isinstance(val, ast.Constant):
                return True
            if isinstance(val, (ast.List, ast.Dict, ast.Set)) and not val.elts:
                return True
            if isinstance(val, ast.Tuple) and not val.elts:
                return True
            if isinstance(val, ast.Name) and val.id in ("None", "True", "False"):
                return True
            return False

        def _handler_returns_default(body: list[ast.stmt]) -> bool:
            for stmt in body:
                if isinstance(stmt, ast.Return) and _is_default_value(stmt.value):
                    return True
                if isinstance(stmt, ast.Assign) and _is_default_value(stmt.value):
                    return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue

            exc_name: str | None = None
            if node.type is None:
                exc_name = "Bare"
            elif isinstance(node.type, ast.Name):
                exc_name = node.type.id
            elif isinstance(node.type, ast.Attribute):
                exc_name = node.type.attr

            if exc_name not in ("Bare", "Exception", "JSONDecodeError", "YAMLError"):
                continue

            if _has_logging_call(node.body):
                continue
            if not _handler_returns_default(node.body):
                continue

            issues.append(CraftIssue(
                line=node.lineno,
                column=node.col_offset,
                code="silent_except_return",
                message="Exception handler silently catches and returns a default value without logging.",
                suggestion="Log the exception or re-raise it. Do not silently return default values.",
                severity=CraftIssueSeverity.SOFT,
            ))

        # generic_name_density
        count = _generic_name_count(tree, _GENERIC_NAMES)
        if count >= 5:
            issues.append(CraftIssue(
                line=1,
                column=0,
                code="generic_name_density",
                message=f"File uses {count} generic names ('data', 'result', 'item', etc.).",
                suggestion="Use more specific, domain-shaped variable and parameter names.",
                severity=CraftIssueSeverity.SOFT,
            ))

        issues.extend(self._check_destructive_operations(tree, capsule))
        issues.extend(self._check_extra_public_api(tree, capsule))
        issues.extend(self._check_schema_fidelity(tree, capsule))
        issues.extend(self._check_empty_ceremony_class(tree))
        issues.extend(self._check_forbidden_public_methods(tree, capsule))
        issues.extend(self._check_forbidden_calls(tree, capsule))
        issues.extend(self._check_scaffold_smell(tree, capsule, source_lines))

        return issues

    def _check_destructive_operations(self, tree: ast.AST, capsule: ProposalCapsule) -> list[CraftIssue]:
        issues = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # shutil.rmtree(...)
            if (isinstance(func, ast.Attribute) and func.attr == "rmtree"
                    and isinstance(func.value, ast.Name) and func.value.id == "shutil"):
                issues.append(self._destructive_issue(node, "shutil.rmtree()"))
                continue

            # direct rmtree(...) from "from shutil import rmtree"
            if isinstance(func, ast.Name) and func.id == "rmtree":
                issues.append(self._destructive_issue(node, "rmtree()"))
                continue

            # os.remove(...), os.unlink(...), os.rmdir(...), os.removedirs(...)
            if (isinstance(func, ast.Attribute) and func.attr in ("remove", "unlink", "rmdir", "removedirs")
                    and isinstance(func.value, ast.Name) and func.value.id == "os"):
                issues.append(self._destructive_issue(node, f"os.{func.attr}()"))
                continue

            # Path(...).unlink() or Path(...).rmdir()
            if (isinstance(func, ast.Attribute) and func.attr in ("unlink", "rmdir")
                    and isinstance(func.value, ast.Call)
                    and isinstance(func.value.func, ast.Name)
                    and func.value.func.id == "Path"):
                issues.append(self._destructive_issue(node, f"Path(...).{func.attr}()"))
                continue

        return issues

    def _destructive_issue(self, node: ast.Call, label: str) -> CraftIssue:
        return CraftIssue(
            line=node.lineno,
            column=node.col_offset,
            code="destructive_operation",
            message=f"{label} call in new Aura-owned file. Destructive filesystem operations require explicit specification.",
            suggestion="Remove the destructive operation unless the task explicitly requires it.",
            severity=CraftIssueSeverity.SOFT,
        )

    def _check_extra_public_api(self, tree: ast.AST, capsule: ProposalCapsule) -> list[CraftIssue]:
        if not capsule.expected_public_symbols:
            return []
        expected = set(capsule.expected_public_symbols)
        found = set()

        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                found.add(node.name)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                        found.add(f"{node.name}.{item.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                found.add(node.name)

        extra = found - expected
        if not extra:
            return []
        return [CraftIssue(
            line=1, column=0,
            code="extra_public_api",
            message=f"Unexpected public symbol(s): {', '.join(sorted(extra))}. Expected: {', '.join(sorted(expected))}",
            suggestion="Remove unexpected public symbols or add them to the expected set if required.",
            severity=CraftIssueSeverity.SOFT,
        )]

    def _check_schema_fidelity(self, tree: ast.AST, capsule: ProposalCapsule) -> list[CraftIssue]:
        if not capsule.expected_dataclass_fields:
            return []
        issues = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name not in capsule.expected_dataclass_fields:
                continue
            expected = set(capsule.expected_dataclass_fields[node.name])
            actual = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    actual.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name) and not t.id.startswith("_"):
                            actual.add(t.id)
            missing = expected - actual
            if missing:
                issues.append(CraftIssue(
                    line=node.lineno, column=node.col_offset,
                    code="schema_field_missing",
                    message=f"Class '{node.name}' missing expected fields: {', '.join(sorted(missing))}",
                    suggestion=f"Add the missing fields: {', '.join(sorted(missing))}",
                    severity=CraftIssueSeverity.SOFT,
                ))
            extra = actual - expected
            if extra:
                issues.append(CraftIssue(
                    line=node.lineno, column=node.col_offset,
                    code="schema_field_extra",
                    message=f"Class '{node.name}' has unexpected fields: {', '.join(sorted(extra))}",
                    suggestion=f"Remove unexpected fields or rename to match expected: {', '.join(sorted(expected))}",
                    severity=CraftIssueSeverity.SOFT,
                ))
        return issues

    def _check_empty_ceremony_class(self, tree: ast.AST) -> list[CraftIssue]:
        issues = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.bases:
                continue
            has_methods = any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) for m in node.body)
            has_fields = any(isinstance(m, ast.AnnAssign) for m in node.body)
            has_public_assigns = any(
                isinstance(m, ast.Assign)
                and any(isinstance(t, ast.Name) and not t.id.startswith("_") for t in m.targets)
                for m in node.body
            )
            if not has_methods and not has_fields and not has_public_assigns:
                issues.append(CraftIssue(
                    line=node.lineno,
                    column=node.col_offset,
                    code="empty_ceremony_class",
                    message=f"Class '{node.name}' has no methods, fields, or state.",
                    suggestion="Remove the class or give it real responsibility.",
                    severity=CraftIssueSeverity.SOFT,
                ))
        return issues

    def _check_forbidden_public_methods(self, tree: ast.AST, capsule: ProposalCapsule) -> list[CraftIssue]:
        if not capsule.forbidden_public_methods:
            return []
        forbidden = set(capsule.forbidden_public_methods)
        issues = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                issues.append(CraftIssue(
                    line=node.lineno,
                    column=node.col_offset,
                    code="forbidden_public_method",
                    message=f"Function '{node.name}' is not allowed in this context.",
                    suggestion=f"Remove '{node.name}' or rename it.",
                    severity=CraftIssueSeverity.SOFT,
                ))
        return issues

    @staticmethod
    def _resolve_call_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            left = CraftEngine._resolve_call_name(func.value)
            if left is not None:
                return f"{left}.{func.attr}"
            return func.attr
        return None

    def _check_forbidden_calls(self, tree: ast.AST, capsule: ProposalCapsule) -> list[CraftIssue]:
        if not capsule.forbidden_calls:
            return []
        forbidden = set(capsule.forbidden_calls)
        issues = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = self._resolve_call_name(node.func)
            if name is not None and name in forbidden:
                issues.append(CraftIssue(
                    line=node.lineno,
                    column=node.col_offset,
                    code="forbidden_call",
                    message=f"Call to '{name}' is not allowed in this context.",
                    suggestion=f"Remove the call to '{name}'.",
                    severity=CraftIssueSeverity.SOFT,
                ))
        return issues

    def _check_scaffold_smell(self, tree: ast.AST, capsule: ProposalCapsule, source_lines: list[str]) -> list[CraftIssue]:
        signals = []

        banner_count = 0
        for line in source_lines:
            stripped = line.strip()
            if re.match(r'^#[=*\-~]{4,}$', stripped):
                banner_count += 1
        if banner_count >= 3:
            signals.append(f"{banner_count} decorative banner lines")

        vague = {"process", "handle", "do_stuff", "run", "execute", "perform", "action", "thing", "item", "data", "info"}
        vague_found = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.lower() in vague and not node.name.startswith("_"):
                    vague_found.append(node.name)
        if vague_found:
            signals.append(f"vague method names: {vague_found}")

        if capsule.expected_public_symbols:
            expected = set(capsule.expected_public_symbols)
            found = set()
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    found.add(node.name)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                            found.add(f"{node.name}.{item.name}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    found.add(node.name)
            extra = found - expected
            if extra:
                signals.append("extra public symbols")

        if len(signals) >= 2:
            return [CraftIssue(
                line=1, column=0,
                code="scaffold_smell",
                message=f"File has generated scaffold texture: {'; '.join(signals)}",
                suggestion="Remove decorative banners and unnecessary infrastructure structure.",
                severity=CraftIssueSeverity.SOFT,
            )]
        return []
