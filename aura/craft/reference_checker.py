import ast
import builtins
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .types import CraftIssue, CraftIssueSeverity


@dataclass
class _FuncSig:
    """Lightweight function/method signature for workspace index."""
    name: str
    params: list[str] = field(default_factory=list)
    defaults_count: int = 0
    has_varargs: bool = False
    has_kwargs: bool = False
    is_method: bool = False


@dataclass
class _ClassInfo:
    """Lightweight class info for workspace index."""
    name: str
    bases: list[str] = field(default_factory=list)
    methods: dict[str, _FuncSig] = field(default_factory=dict)
    attributes: set[str] = field(default_factory=set)


def _has_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Check if a function node has a decorator with the given name."""
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == name:
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == name:
            return True
    return False


class ReferenceChecker:
    def __init__(self):
        self._workspace_symbols: dict[str, set[str]] | None = None
        self._workspace_modules: set[str] | None = None
        self._workspace_root: Path | None = None
        # Phase 4: richer signature/class data
        self._workspace_functions: dict[str, _FuncSig] = {}       # module.funcname -> sig
        self._workspace_classes: dict[str, _ClassInfo] = {}       # module.ClassName -> info

    # ------------------------------------------------------------------
    # Workspace index
    # ------------------------------------------------------------------

    def _build_workspace_index(self, workspace_root: Path) -> None:
        if self._workspace_symbols is not None and self._workspace_root == workspace_root:
            return

        self._workspace_root = workspace_root
        self._workspace_symbols = {}
        self._workspace_modules = set()
        self._workspace_functions = {}
        self._workspace_classes = {}

        for py_file in workspace_root.rglob("*.py"):
            try:
                rel_path = py_file.relative_to(workspace_root)
                parts = list(rel_path.parts)
                if parts[-1] == "__init__.py":
                    parts.pop()
                else:
                    parts[-1] = parts[-1][:-3]

                module_path = ".".join(parts) if parts else ""
                if module_path:
                    self._workspace_modules.add(module_path)

                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content)
                symbols = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        symbols.add(node.name)
                    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                        symbols.add(node.id)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            symbols.add(alias.asname or alias.name.split('.')[0])
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            symbols.add(alias.asname or alias.name)

                if module_path:
                    self._workspace_symbols[module_path] = symbols

                # Phase 4: collect function signatures and class info
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        sig = self._extract_signature(node)
                        key = f"{module_path}.{node.name}" if module_path else node.name
                        self._workspace_functions[key] = sig
                    elif isinstance(node, ast.ClassDef):
                        ci = self._extract_class_info(node)
                        key = f"{module_path}.{node.name}" if module_path else node.name
                        self._workspace_classes[key] = ci
                        # Also index methods as module.ClassName.method
                        for mname, msig in ci.methods.items():
                            mkey = f"{key}.{mname}"
                            self._workspace_functions[mkey] = msig
            except Exception:
                continue

    @staticmethod
    def _extract_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool = False) -> _FuncSig:
        params: list[str] = []
        defaults_count = 0
        has_varargs = False
        has_kwargs = False

        for arg in node.args.args:
            params.append(arg.arg)
        if node.args.vararg:
            has_varargs = True
            params.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            has_kwargs = True
            params.append(f"**{node.args.kwarg.arg}")

        # Count defaults (they align with the *last* positional params)
        defaults_count = len(node.args.defaults)

        return _FuncSig(
            name=node.name,
            params=params,
            defaults_count=defaults_count,
            has_varargs=has_varargs,
            has_kwargs=has_kwargs,
            is_method=is_method,
        )

    @staticmethod
    def _extract_class_info(node: ast.ClassDef) -> _ClassInfo:
        bases: list[str] = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))

        methods: dict[str, _FuncSig] = {}
        attributes: set[str] = set()

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_method = not _has_decorator(child, "staticmethod")
                methods[child.name] = ReferenceChecker._extract_signature(child, is_method=is_method)
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        attributes.add(target.id)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                attributes.add(child.target.id)

        return _ClassInfo(name=node.name, bases=bases, methods=methods, attributes=attributes)

    # ------------------------------------------------------------------
    # Main check entry point
    # ------------------------------------------------------------------

    def check(self, capsule, workspace_root: Path | None = None) -> list[CraftIssue]:
        if getattr(capsule, "language", "python") != "python":
            return []

        if workspace_root:
            self._build_workspace_index(workspace_root)

        try:
            tree = ast.parse(capsule.proposed_code)
        except SyntaxError:
            return []

        local_defs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_defs.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local_defs.add(node.id)
            elif isinstance(node, ast.arg):
                local_defs.add(node.arg)

        imported_names: dict[str, str] = {}
        import_sources: list[tuple[str, ast.AST, str | None]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split('.')[0]
                    imported_names[name] = alias.name
                    import_sources.append((alias.name, node, None))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imported_names[name] = f"{node.module}.{alias.name}"
                        import_sources.append((node.module, node, alias.name))

        issues: list[CraftIssue] = []
        builtin_names = set(dir(builtins))

        # 1. Undefined-name check
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
                if name in builtin_names:
                    continue
                if name in local_defs:
                    continue
                if name in imported_names:
                    continue

                issues.append(
                    CraftIssue(
                        line=node.lineno,
                        column=node.col_offset,
                        code="undefined-name",
                        message=f"Name '{name}' is used but never defined or imported.",
                        suggestion="Define or import the name before using it.",
                        severity=CraftIssueSeverity.HARD,
                    )
                )

        # 2. Broken-import check
        stdlib_modules = getattr(sys, "stdlib_module_names", set())

        for mod_path, node, symbol in import_sources:
            base_module = mod_path.split('.')[0]
            resolved = False

            if base_module in stdlib_modules or mod_path in stdlib_modules:
                resolved = True

            if not resolved:
                try:
                    if importlib.util.find_spec(base_module) is not None:
                        resolved = True
                except Exception:
                    resolved = False

            if not resolved and self._workspace_modules is not None:
                if mod_path in self._workspace_modules or base_module in self._workspace_modules:
                    resolved = True

            if not resolved and workspace_root:
                rel_path = mod_path.replace('.', '/')
                if (workspace_root / f"{rel_path}.py").exists() or (workspace_root / rel_path / "__init__.py").exists():
                    resolved = True

            if not resolved:
                issues.append(
                    CraftIssue(
                        line=node.lineno,
                        column=node.col_offset,
                        code="broken-import",
                        message=f"Import source '{mod_path}' could not be resolved in workspace or stdlib.",
                        suggestion="Install the missing package or correct the import path.",
                        severity=CraftIssueSeverity.HARD,
                    )
                )
            elif symbol and symbol != "*":
                if self._workspace_symbols and mod_path in self._workspace_symbols:
                    if symbol not in self._workspace_symbols[mod_path]:
                        issues.append(
                            CraftIssue(
                                line=node.lineno,
                                column=node.col_offset,
                                code="broken-import",
                                message=f"Import source '{mod_path}' has no symbol '{symbol}'.",
                                suggestion="Check the spelling or define the missing symbol.",
                                severity=CraftIssueSeverity.HARD,
                            )
                        )
                elif base_module in stdlib_modules:
                    try:
                        mod = __import__(mod_path, fromlist=[symbol])
                        if not hasattr(mod, symbol):
                            issues.append(
                                CraftIssue(
                                    line=node.lineno,
                                    column=node.col_offset,
                                    code="broken-import",
                                    message=f"Import source '{mod_path}' has no symbol '{symbol}'.",
                                    suggestion="Check the spelling or correct the import path.",
                                    severity=CraftIssueSeverity.HARD,
                                )
                            )
                    except Exception:
                        continue

        # 3. Phase 4: signature mismatch and attribute checks
        if self._workspace_functions or self._workspace_classes:
            issues.extend(self._check_calls(tree, local_defs, imported_names))

        return issues

    # ------------------------------------------------------------------
    # Phase 4: Call signature & attribute checks
    # ------------------------------------------------------------------

    def _check_calls(
        self,
        tree: ast.Module,
        local_defs: set[str],
        imported_names: dict[str, str],
    ) -> list[CraftIssue]:
        issues: list[CraftIssue] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                issue = self._check_single_call(node, local_defs, imported_names)
                if issue:
                    issues.append(issue)

            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                issue = self._check_attribute(node, local_defs, imported_names)
                if issue:
                    issues.append(issue)

        return issues

    def _resolve_callee(
        self,
        func_node: ast.expr,
        local_defs: set[str],
        imported_names: dict[str, str],
    ) -> tuple[str | None, str | None]:
        """Resolve a call target to (module_path, symbol_name) if possible.

        Returns (None, None) when we cannot resolve to a workspace symbol.
        """
        if isinstance(func_node, ast.Name):
            name = func_node.id
            if name in local_defs:
                return None, None  # local, not in workspace index
            if name in imported_names:
                full = imported_names[name]
                parts = full.rsplit(".", 1)
                if len(parts) == 2:
                    return parts[0], parts[1]
                return None, parts[0]
            # Bare name not imported — try workspace modules
            if self._workspace_functions:
                for key in self._workspace_functions:
                    if key.endswith(f".{name}") or key == name:
                        parts = key.rsplit(".", 1)
                        if len(parts) == 2:
                            return parts[0], parts[1]
                        return None, parts[0]
            return None, None

        elif isinstance(func_node, ast.Attribute):
            # obj.method() — try to resolve obj to a known class
            obj_name = self._resolve_obj_name(func_node.value)
            if obj_name and self._workspace_classes:
                for cls_key, cls_info in self._workspace_classes.items():
                    if cls_key.endswith(f".{obj_name}") or cls_key == obj_name:
                        return cls_key, func_node.attr
            return None, None

        return None, None

    @staticmethod
    def _resolve_obj_name(node: ast.expr) -> str | None:
        """Best-effort resolve an expression to a simple name for class lookup."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _check_single_call(
        self,
        call_node: ast.Call,
        local_defs: set[str],
        imported_names: dict[str, str],
    ) -> CraftIssue | None:
        module_path, symbol = self._resolve_callee(call_node.func, local_defs, imported_names)
        if module_path is None and symbol is None:
            return None

        # Look up the function signature
        sig = self._find_signature(module_path, symbol)
        if sig is None:
            return None

        # Count positional arguments (args before any keyword)
        pos_args = 0
        for arg in call_node.args:
            if isinstance(arg, ast.Starred):
                # *args unpacking — can't count, skip
                return None
            pos_args += 1

        # Count required positional params (total params minus defaults, minus *args/**kwargs)
        param_list = list(sig.params)
        # For methods, skip the first param (self/cls) — it's passed implicitly
        if sig.is_method and param_list:
            param_list.pop(0)
            # Adjust defaults_count: if the removed param had a default, reduce count
            if sig.defaults_count > 0 and sig.defaults_count == len(sig.params) - (len(param_list)):
                pass  # defaults align with last params; removing first doesn't change count
            # Actually, defaults always align with the last N params. Removing self (first param)
            # doesn't change how many defaults there are, but the total param count drops by 1.
            # If defaults_count exceeded the new param count, cap it.
            effective_defaults = min(sig.defaults_count, len(param_list))
        else:
            effective_defaults = sig.defaults_count

        total_params = len(param_list)
        required = total_params - effective_defaults
        if sig.has_varargs:
            required -= 1  # *args param
        if sig.has_kwargs:
            required -= 1  # **kwargs param

        # If the function has *args, any number of positional args is fine
        if sig.has_varargs:
            return None

        max_pos = total_params
        if sig.has_kwargs:
            max_pos -= 1

        keyword_names = {kw.arg for kw in call_node.keywords if kw.arg is not None}
        # Params covered by keywords reduce the positional requirement
        params_covered_by_kw = 0
        for p in param_list:
            if p in keyword_names:
                params_covered_by_kw += 1

        effective_required = max(0, required - params_covered_by_kw)
        effective_max = max_pos - params_covered_by_kw

        if pos_args < effective_required:
            display_name = symbol if module_path is None else f"{module_path}.{symbol}"
            return CraftIssue(
                line=call_node.lineno,
                column=call_node.col_offset,
                code="call-signature",
                message=(
                    f"Function '{display_name}' expects at least {effective_required} "
                    f"positional argument(s) but {pos_args} were provided."
                ),
                suggestion="Provide the required positional arguments.",
                severity=CraftIssueSeverity.HARD,
            )

        if pos_args > effective_max:
            display_name = symbol if module_path is None else f"{module_path}.{symbol}"
            return CraftIssue(
                line=call_node.lineno,
                column=call_node.col_offset,
                code="call-signature",
                message=(
                    f"Function '{display_name}' accepts at most {effective_max} "
                    f"positional argument(s) but {pos_args} were provided."
                ),
                suggestion="Remove excess positional arguments or use keyword arguments.",
                severity=CraftIssueSeverity.HARD,
            )

        return None

    def _find_signature(self, module_path: str | None, symbol: str) -> _FuncSig | None:
        """Find a function signature in the workspace index."""
        # Direct lookup: module.symbol
        if module_path:
            key = f"{module_path}.{symbol}"
            if key in self._workspace_functions:
                return self._workspace_functions[key]

        # Try bare symbol across all modules
        for key, sig in self._workspace_functions.items():
            if key.endswith(f".{symbol}") or key == symbol:
                return sig

        if module_path:
            cls_key = f"{module_path}.{symbol}"
            if cls_key in self._workspace_classes:
                return self._workspace_classes[cls_key].methods.get("__init__")
        else:
            for cls_key, cls_info in self._workspace_classes.items():
                if cls_key.endswith(f".{symbol}") or cls_key == symbol:
                    return cls_info.methods.get("__init__")

        return None

    def _check_attribute(
        self,
        attr_node: ast.Attribute,
        local_defs: set[str],
        imported_names: dict[str, str],
    ) -> CraftIssue | None:
        """Check that obj.attr exists when obj resolves to a known workspace class."""
        obj_name = self._resolve_obj_name(attr_node.value)
        if obj_name is None:
            return None

        # Find the class in workspace index
        cls_info = None
        for cls_key, ci in self._workspace_classes.items():
            if cls_key.endswith(f".{obj_name}") or cls_key == obj_name:
                cls_info = ci
                break

        if cls_info is None:
            return None

        attr = attr_node.attr
        # Check methods and attributes
        if attr in cls_info.methods or attr in cls_info.attributes:
            return None

        if attr.startswith("__") and attr.endswith("__"):
            return None

        # If the class has bases, we can't be sure — skip
        if cls_info.bases:
            return None

        return CraftIssue(
            line=attr_node.lineno,
            column=attr_node.col_offset,
            code="missing-attribute",
            message=f"Class '{obj_name}' has no attribute '{attr}'.",
            suggestion="Check the attribute name or define it on the class.",
            severity=CraftIssueSeverity.HARD,
        )
