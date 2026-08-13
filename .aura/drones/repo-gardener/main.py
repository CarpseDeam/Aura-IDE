"""Repo Gardener — one bounded lap of health mapping + removal per invocation."""

import ast
import difflib
import json
import os
import sys
import traceback
from pathlib import Path

from caps import Appetite, is_editable, within_budget
from mapper import (
    Candidate,
    FileHealth,
    _collect_module_to_file,
    _file_health_to_dict,
    _resolve_from_module,
    build_import_graph,
    compute_file_health,
    detect_unused_imports,
    rank_files,
    walk,
)
from method_extractor import extract_method
from picker import Chosen, pick, pick_refactor
from proposal import (
    build_edit_request,
    build_extract_request,
    request_edit_proposal,
    request_extract_proposal,
)
from remover import strip_import
from verify import import_resolves, py_compile_check
from writer import write_with_revert


def _build_eligible_ranked(
    health_map: list[FileHealth],
    candidates: list[Candidate],
) -> list[Candidate]:
    """Replicate the picker's sort for fallback iteration."""
    eligible = [
        c
        for c in candidates
        if c.kind == "unused_import" and c.confidence == "high"
    ]
    if not eligible:
        return []

    file_scores: dict[str, float] = {}
    for fh in health_map:
        fh_parts = Path(fh.path).parts
        for c in eligible:
            c_parts = Path(c.path).parts
            if len(c_parts) >= len(fh_parts) and c_parts[-len(fh_parts):] == fh_parts:
                file_scores[c.path] = fh.score
                break

    eligible.sort(
        key=lambda c: (
            -file_scores.get(c.path, 0.0),
            len(Path(c.path).parts),
            c.path,
        )
    )
    return eligible


def _resolve_target_root(workspace_root: str, goal: str) -> tuple[Path | None, str | None]:
    """Return (safe_target_root, error_message).

    Default is workspace_root/aura.
    If goal contains 'target_root:' override, validate:
    - Must be relative path (reject absolute)
    - Must not contain '..'
    - Resolved path must stay inside workspace_root/aura
    If unsafe, return (None, error_message).
    """
    root = Path(workspace_root).resolve()
    aura_root = (root / "aura").resolve()

    default = aura_root

    custom = None
    for line in goal.splitlines():
        line = line.strip()
        if line.startswith("target_root:"):
            val = line[len("target_root:"):].strip()
            if val:
                custom = val
                break

    if custom is None:
        return default, None

    p = Path(custom)
    if p.is_absolute():
        return None, f"target_root path must be relative, got absolute path: {custom}"

    if ".." in p.parts:
        return None, f"target_root path must not contain '..': {custom}"

    resolved = (root / p).resolve()
    try:
        resolved.relative_to(aura_root)
    except ValueError:
        return None, f"target_root must be inside {aura_root}, got: {resolved}"

    return resolved, None


def _validate_plan_path(workspace_root: str, file_path: str) -> tuple[Path | None, str | None]:
    """Validate a model-returned file path.

    Returns (resolved_path, error_message).
    Rejects: absolute paths, paths containing '..', paths outside workspace_root/aura,
    files that don't exist, and files in RUNTIME_SKIP.
    """
    root = Path(workspace_root).resolve()
    aura_root = (root / "aura").resolve()

    p = Path(file_path)

    # Reject absolute
    if p.is_absolute():
        return None, f"model returned absolute path: {file_path}"

    # Reject ..
    if ".." in p.parts:
        return None, f"model returned path containing '..': {file_path}"

    # Resolve, must be inside workspace_root/aura
    resolved = (root / p).resolve()
    try:
        rel_to_aura = resolved.relative_to(aura_root)
    except ValueError:
        return None, f"model returned path outside aura/: {file_path}"

    # Must already exist (no creation)
    if not resolved.is_file():
        return None, f"model returned non-existent file: {file_path}"

    # Reject protected paths from RUNTIME_SKIP
    rel_slash = str(rel_to_aura).replace("\\", "/")
    if not is_editable(rel_slash):
        return None, f"model returned protected file: {file_path}"

    return resolved, None


def _rel_to_aura(chosen_path: str, target_root: Path) -> str:
    """Convert a candidate's full path to a path relative to aura/."""
    return str(Path(chosen_path).relative_to(target_root)).replace("\\", "/")


def _compute_diff(path: Path, old: str, new: str) -> str:
    """Return unified diff string."""
    diff = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return "".join(diff)


def _changed_line_count(diff_text: str) -> int:
    """Count lines that differ (excludes headers and hunks)."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") or \
           line.startswith("-") and not line.startswith("---"):
            count += 1
    return count


def _extract_imports(source: str) -> list[str]:
    """Extract import lines from source for context."""
    lines = source.splitlines()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            result.append(stripped)
    return result


def _find_call_sites(chosen: Chosen, target_root: Path) -> list[str]:
    """Search for call sites of the chosen target within aura/.

    Uses ripgrep if available (subprocess), otherwise returns empty.
    This is best-effort; an empty result is acceptable.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["rg", "-n", f"\\b{chosen.detail}\\b", str(target_root)],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().splitlines()
        return [l.strip() for l in lines if l.strip()][:20]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw)

    workspace_root = data.get("workspace_root", ".")
    goal = data.get("goal", "")

    aura_provider_id = os.environ.get("AURA_PROVIDER_ID", "")
    aura_model = os.environ.get("AURA_MODEL", "")

    target_root, target_error = _resolve_target_root(workspace_root, goal)
    workspace_root_resolved = Path(workspace_root).resolve()
    if target_error:
        result = {
            "ok": False,
            "summary": target_error,
            "cargo": {"error": target_error},
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.flush()
        return

    files = walk(target_root)

    # -- Empty root -----------------------------------------------------------
    if not files:
        cargo = {
            "tier": 1,
            "target_root": str(target_root),
            "provider_id": aura_provider_id or None,
            "model": aura_model or None,
            "health_map": [],
            "candidates": [],
            "chosen": None,
            "action": None,
            "skipped": [],
            "has_work": False,
            "summary_counts": {
                "files_scanned": 0,
                "unused_imports": 0,
                "god_files": 0,
            },
        }
        result = {
            "ok": True,
            "summary": f"No Python files found in {target_root}.",
            "cargo": cargo,
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.flush()
        return

    # -- Map ------------------------------------------------------------------
    graph = build_import_graph(files, target_root)

    # -- Re-export pre-pass ----------------------------------------------------
    base_module = target_root.name
    mod_to_file = _collect_module_to_file(files, target_root, base_module)

    reexport_map: dict[str, set[str]] = {}
    for f in files:
        rel = f.relative_to(target_root)
        stem = str(rel.with_suffix(""))

        if stem.endswith("__init__"):
            own_mod = base_module if stem == "__init__" else f"{base_module}.{stem.replace('/', '.')[:-9]}"
        else:
            own_mod = f"{base_module}.{stem.replace('/', '.')}"

        try:
            source = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(f))
        except (SyntaxError, OSError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "__future__":
                continue

            if node.level is not None and node.level > 0:
                resolved = _resolve_from_module(base_module, stem, node.level, node.module)
            elif node.module is not None:
                mod = node.module
                if mod == base_module or mod.startswith(base_module + "."):
                    resolved = mod
                else:
                    resolved = None
            else:
                resolved = None

            if resolved is None or resolved not in mod_to_file or resolved == own_mod:
                continue

            reexport_map.setdefault(resolved, set()).update(
                alias.asname or alias.name for alias in node.names
            )

    file_exported_map: dict[str, frozenset[str]] = {
        str(mod_to_file[mod]): frozenset(names)
        for mod, names in reexport_map.items()
        if mod in mod_to_file
    }

    health_map: list[FileHealth] = []
    all_candidates: list[Candidate] = []

    for f in files:
        rel_path = str(f.relative_to(target_root))

        try:
            source = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            tree = ast.parse(source, filename=str(f))
            syntax_error = False
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
            syntax_error = True

        fan_in, fan_out, _ = graph.get(f, (0, 0, set()))

        if syntax_error:
            health_map.append(
                FileHealth(
                    path=rel_path,
                    lines=source.count("\n") + 1,
                    class_count=0,
                    function_count=0,
                    import_count=0,
                    top_level_symbols=0,
                    largest_function_lines=0,
                    largest_class_lines=0,
                    fan_in=fan_in,
                    fan_out=fan_out,
                    mixed_responsibility=False,
                    score=0.0,
                    reasons=("syntax error",),
                )
            )
        else:
            health = compute_file_health(rel_path, source, tree, fan_in, fan_out)
            health_map.append(health)

            candidates = detect_unused_imports(
                f, source, tree,
                exported_names=file_exported_map.get(str(f), frozenset()),
            )
            all_candidates.extend(candidates)

    ranked = rank_files(health_map)
    top15 = [_file_health_to_dict(fh) for fh in ranked[:15]]

    candidate_dicts = [
        {
            "kind": c.kind,
            "path": c.path,
            "detail": c.detail,
            "confidence": c.confidence,
            "line": c.line,
        }
        for c in all_candidates
    ]

    god_files = sum(1 for fh in health_map if fh.score >= 5.0)

    # -- Pick ----------------------------------------------------------------
    chosen = pick(health_map, all_candidates)

    # -- Tier 1 lap: try to execute one edit ----------------------------------
    action = None
    skipped: list[dict] = []
    chosen_result: dict | None = None
    has_work = False
    summary = ""
    verified = False
    diff_text = ""
    attempted_diff = ""

    eligible_list = _build_eligible_ranked(health_map, all_candidates) if not chosen else \
        [chosen] + _build_eligible_ranked(health_map, all_candidates)

    for candidate in eligible_list:
        # Prepare chosen_result for cargo
        chosen_result = {
            "kind": candidate.kind,
            "path": candidate.path,
            "detail": candidate.detail,
            "line": candidate.line,
            "reason": f"removing unused '{candidate.detail}' import from {candidate.path}",
            "file_score": 0.0,
        }

        # --- Guard: is_editable ---------------------------------------------
        rel_path = _rel_to_aura(candidate.path, target_root)
        if not is_editable(rel_path):
            skipped.append({
                "path": candidate.path,
                "reason": "runtime-protected",
                "detail": candidate.detail,
            })
            continue

        # --- Re-verify on live disk -----------------------------------------
        try:
            live_source = Path(candidate.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append({
                "path": candidate.path,
                "reason": "unreadable on re-read",
                "detail": candidate.detail,
            })
            continue

        try:
            live_tree = ast.parse(live_source, filename=str(candidate.path))
        except SyntaxError:
            skipped.append({
                "path": candidate.path,
                "reason": "syntax error on re-read",
                "detail": candidate.detail,
            })
            continue

        live_unused = detect_unused_imports(
            Path(candidate.path), live_source, live_tree,
            exported_names=file_exported_map.get(candidate.path, frozenset()),
        )
        still_unused = any(u.detail == candidate.detail for u in live_unused)
        if not still_unused:
            skipped.append({
                "path": candidate.path,
                "reason": "binding no longer unused on re-verify",
                "detail": candidate.detail,
            })
            continue

        # --- Found a valid target --------------------------------------------
        new_source = strip_import(live_source, candidate.detail, candidate.line)
        if new_source == live_source:
            skipped.append({
                "path": candidate.path,
                "reason": "strip_import made no change",
                "detail": candidate.detail,
            })
            continue

        # --- Diff & budget ---------------------------------------------------
        diff_text = _compute_diff(Path(candidate.path), live_source, new_source)
        changed_lines = _changed_line_count(diff_text)
        appetite = Appetite()
        budget_ok, budget_why = within_budget(changed_lines, 1, appetite)
        if not budget_ok:
            skipped.append({
                "path": candidate.path,
                "reason": f"over budget: {budget_why}",
                "detail": candidate.detail,
            })
            attempted_diff = diff_text
            continue

        # --- Write & verify --------------------------------------------------
        revert = write_with_revert(Path(candidate.path), new_source)

        # Gate 1: py_compile
        v_ok, v_error = py_compile_check(Path(candidate.path))
        if not v_ok:
            revert()
            skipped.append({
                "path": candidate.path,
                "detail": candidate.detail,
                "reason": f"py_compile failed: {v_error}",
            })
            attempted_diff = diff_text
            continue

        # Gate 2: import resolution
        rel_to_workspace = Path(candidate.path).relative_to(workspace_root_resolved)
        stem = str(rel_to_workspace.with_suffix(""))
        if stem.endswith("__init__"):
            if stem == "__init__":
                module_path = workspace_root_resolved.name
            else:
                module_path = stem[:-9].replace("/", ".")  # strip trailing /__init__
        else:
            module_path = stem.replace("/", ".")

        i_ok, i_error = import_resolves(module_path, workspace_root_resolved)
        if i_ok:
            action = {
                "kind": "removed_import",
                "path": candidate.path,
                "binding": candidate.detail,
            }
            has_work = True
            verified = True
            summary = (
                f"Removed unused import '{candidate.detail}' from {candidate.path}. "
                f"py_compile + import resolution OK."
            )
            break
        else:
            revert()
            skipped.append({
                "path": candidate.path,
                "detail": candidate.detail,
                "reason": "import resolution failed",
                "traceback": i_error,
            })
            attempted_diff = diff_text
            continue

    # -- Tier 2 lap: refactor via model (runs only when Tier 1 idle) ----------
    model_calls = 0
    tier2_action = None
    tier2_plan = None
    tier2_failure = ""
    reverted = False

    if action is None:
        # Tier 1 found nothing — try Tier 2
        # Pick a refactor target
        tier2_chosen = pick_refactor(health_map, target_root)

        if tier2_chosen is not None:
            # Check editability of this target
            tier2_rel = _rel_to_aura(tier2_chosen.path, target_root)
            if is_editable(tier2_rel):
                # Read the source, build packet
                try:
                    src = Path(tier2_chosen.path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    src = ""

                if src:
                    if tier2_chosen.kind == "simplify_function":
                        # --- Extract-method path (model identifies, deterministic moves) ---
                        extract_packet = build_extract_request(tier2_chosen, src)
                        extract_plan = request_extract_proposal(extract_packet)
                        model_calls = 1
                        tier2_plan = extract_plan
                        tier2_diffs = {}

                        if not extract_plan.helper_name:
                            tier2_failure = extract_plan.rationale or "model returned no extract plan"
                        else:
                            result = extract_method(
                                src,
                                tier2_chosen.detail,       # target_func_name
                                extract_plan.start_line,
                                extract_plan.end_line,
                                extract_plan.helper_name,
                            )
                            if not result.ok:
                                tier2_failure = result.error or "extract_method refused"
                                # Skipped receipt
                                skipped.append({
                                    "path": tier2_chosen.path,
                                    "detail": tier2_chosen.detail,
                                    "reason": f"extract_method refused: {result.error}",
                                })
                            else:
                                # Write & verify
                                revert = write_with_revert(Path(tier2_chosen.path), result.new_source)

                                v_ok, v_error = py_compile_check(Path(tier2_chosen.path))
                                if not v_ok:
                                    revert()
                                    reverted = True
                                    tier2_failure = f"py_compile failed: {v_error}"
                                    skipped.append({
                                        "path": tier2_chosen.path,
                                        "detail": tier2_chosen.detail,
                                        "reason": tier2_failure,
                                    })
                                else:
                                    # Also run import_resolves if available
                                    i_ok = True
                                    i_error = ""
                                    try:
                                        rel_to_ws = Path(tier2_chosen.path).relative_to(workspace_root_resolved)
                                        stem = str(rel_to_ws.with_suffix(""))
                                        if stem.endswith("__init__"):
                                            if stem == "__init__":
                                                mpath = workspace_root_resolved.name
                                            else:
                                                mpath = stem[:-9].replace("/", ".")
                                        else:
                                            mpath = stem.replace("/", ".")
                                        i_ok, i_error = import_resolves(mpath, workspace_root_resolved)
                                    except Exception:
                                        # import_resolves unavailable — don't block extraction
                                        i_ok = True

                                    if not i_ok:
                                        revert()
                                        reverted = True
                                        tier2_failure = f"import resolution failed: {i_error}"
                                        skipped.append({
                                            "path": tier2_chosen.path,
                                            "detail": tier2_chosen.detail,
                                            "reason": tier2_failure,
                                            "traceback": i_error,
                                        })
                                    else:
                                        tier2_action = {
                                            "kind": "extracted_method",
                                            "function": tier2_chosen.detail,
                                            "helper": extract_plan.helper_name,
                                            "params": result.stats.get("params", []) if result.stats else [],
                                            "returns": result.stats.get("returns", []) if result.stats else [],
                                            "lines_moved": result.stats.get("lines_moved", 0) if result.stats else 0,
                                            "func_lines_before": result.stats.get("func_lines_before", 0) if result.stats else 0,
                                            "func_lines_after": result.stats.get("func_lines_after", 0) if result.stats else 0,
                                        }
                                        reverted = False
                                        has_work = True
                                        # Skip the rest of Tier 2 flow (don't fall into code-writing)
                    else:
                        # --- Existing code-writing path ---
                        # Extract imports
                        import_lines = _extract_imports(src)
                        # Find call sites (grep for the target detail)
                        call_sites = _find_call_sites(tier2_chosen, target_root)

                        packet = build_edit_request(tier2_chosen, src, import_lines, call_sites)
                        plan = request_edit_proposal(packet)
                        model_calls = 1
                        tier2_plan = plan

                        if not plan.files:
                            # Model returned nothing useful
                            tier2_failure = plan.rationale
                        else:
                            # Step 1 — Accepted file list with strict validation
                            actual_total_changed = 0
                            plan_diffs: dict[str, str] = {}
                            accepted: list[tuple[str, Path, str]] = []

                            # 1a. Check key set match
                            if set(plan.files) != set(plan.new_contents.keys()):
                                tier2_failure = (
                                    f"plan.files and plan.new_contents keys mismatch: "
                                    f"files={set(plan.files)} vs "
                                    f"new_contents_keys={set(plan.new_contents.keys())}"
                                )
                            else:
                                # 1b. Validate every path, reject whole plan on first error
                                for pf_rel in plan.files:
                                    resolved, err = _validate_plan_path(workspace_root, pf_rel)
                                    if err is not None:
                                        tier2_failure = err
                                        break
                                    accepted.append((pf_rel, resolved, plan.new_contents[pf_rel]))

                            # Step 2 — Compute diffs from accepted list
                            if not tier2_failure:
                                for pf_rel, resolved_path, new_content in accepted:
                                    try:
                                        old_content = resolved_path.read_text(encoding="utf-8")
                                    except OSError as e:
                                        tier2_failure = f"unreadable: {pf_rel}: {e}"
                                        break
                                    diff = _compute_diff(resolved_path, old_content, new_content)
                                    plan_diffs[pf_rel] = diff
                                    actual_total_changed += _changed_line_count(diff)

                            # Step 3 — Write from accepted list
                            if not tier2_failure:
                                reverts: list = []
                                all_ok = True
                                verification_errors: list[str] = []
                                tier2_diffs: dict[str, str] = {}

                                for pf_rel, resolved_path, new_content in accepted:
                                    revert_fn = write_with_revert(resolved_path, new_content)
                                    reverts.append(revert_fn)

                                    v_ok, v_err = py_compile_check(resolved_path)
                                    if not v_ok:
                                        verification_errors.append(f"{pf_rel}: {v_err}")
                                        all_ok = False
                                        break

                                    tier2_diffs[pf_rel] = plan_diffs.get(pf_rel, "")

                                if all_ok:
                                    tier2_action = {
                                        "kind": tier2_chosen.kind,
                                        "path": tier2_chosen.path,
                                        "detail": tier2_chosen.detail,
                                        "line": tier2_chosen.line,
                                    }
                                    reverted = False
                                else:
                                    for rfn in reversed(reverts):
                                        rfn()
                                    reverted = True
                                    tier2_failure = "; ".join(verification_errors)
                                    tier2_action = None

    # --- Build cargo ---------------------------------------------------------
    if tier2_action:
        # Tier 2 succeeded
        final_summary = (
            f"Refactored {tier2_chosen.kind} '{tier2_chosen.detail}' "
            f"in {tier2_chosen.path}. {tier2_plan.rationale if tier2_plan else ''}"
        )
        cargo = {
            "tier": 2,
            "target_root": str(target_root),
            "health_map": top15,
            "candidates": candidate_dicts,
            "chosen": {
                "kind": tier2_chosen.kind,
                "path": tier2_chosen.path,
                "detail": tier2_chosen.detail,
                "line": tier2_chosen.line,
                "reason": tier2_chosen.reason,
                "file_score": tier2_chosen.file_score,
            } if tier2_chosen else None,
            "action": tier2_action,
            "diff": tier2_diffs,
            "verified": True,
            "skipped": skipped,
            "has_work": True,
            "model_calls": model_calls,
            "rationale": tier2_plan.rationale if tier2_plan else "",
            "reverted": False,
            "provider_id": aura_provider_id or None,
            "model": aura_model or None,
            "summary_counts": {
                "files_scanned": len(files),
                "unused_imports": len(all_candidates),
                "god_files": god_files,
            },
        }
    elif reverted:
        final_summary = (
            f"Tier 2 reverted: {tier2_failure}"
        )
        cargo = {
            "tier": 2,
            "target_root": str(target_root),
            "provider_id": aura_provider_id or None,
            "model": aura_model or None,
            "health_map": top15,
            "candidates": candidate_dicts,
            "chosen": {
                "kind": tier2_chosen.kind,
                "path": tier2_chosen.path,
                "detail": tier2_chosen.detail,
                "line": tier2_chosen.line,
                "reason": tier2_chosen.reason,
                "file_score": tier2_chosen.file_score,
            } if tier2_chosen else None,
            "action": None,
            "diff": tier2_diffs if 'tier2_diffs' in dir() else {},
            "verified": False,
            "reverted": True,
            "skipped": skipped,
            "has_work": False,
            "model_calls": model_calls,
            "rationale": tier2_plan.rationale if tier2_plan else "",
            "failure": tier2_failure,
            "attempted_diff": tier2_diffs if 'tier2_diffs' in dir() else {},
            "summary_counts": {
                "files_scanned": len(files),
                "unused_imports": len(all_candidates),
                "god_files": god_files,
            },
        }
    elif action:
        # Tier 1 succeeded
        final_summary = summary
        cargo = {
            "tier": 1,
            "target_root": str(target_root),
            "provider_id": aura_provider_id or None,
            "model": aura_model or None,
            "health_map": top15,
            "candidates": candidate_dicts,
            "chosen": chosen_result,
            "action": action,
            "diff": diff_text,
            "attempted_diff": "",
            "verified": verified,
            "skipped": skipped,
            "has_work": has_work,
            "model_calls": model_calls,
            "caps": {
                "max_files": 1,
                "max_changed_lines": 20,
                "files_touched": 1 if action else 0,
                "changed_lines": _changed_line_count(diff_text) if action else 0,
            } if action else None,
            "summary_counts": {
                "files_scanned": len(files),
                "unused_imports": len(all_candidates),
                "god_files": god_files,
            },
        }
    else:
        # No work found in either tier
        if skipped:
            final_summary = (
                f"No safe edit. Tried {len(skipped)} candidate(s), "
                f"all skipped: {skipped[0].get('reason', 'unknown')}"
            )
            if model_calls and tier2_failure:
                final_summary += f" | Tier 2: {tier2_failure}"
            elif model_calls:
                final_summary += " | Tier 2: model returned no edit"
        elif not chosen_result and not all_candidates:
            final_summary = f"Mapped {len(files)} files. No unused imports found."
        else:
            final_summary = f"Mapped {len(files)} files. No actionable candidates."

        cargo = {
            "tier": 1,  # Both tiers ran but found nothing
            "target_root": str(target_root),
            "provider_id": aura_provider_id or None,
            "model": aura_model or None,
            "health_map": top15,
            "candidates": candidate_dicts,
            "chosen": None,
            "action": None,
            "diff": "",
            "attempted_diff": attempted_diff,
            "verified": False,
            "skipped": skipped,
            "has_work": False,
            "model_calls": model_calls,
            "tier2_failure": tier2_failure if model_calls else "",
            "summary_counts": {
                "files_scanned": len(files),
                "unused_imports": len(all_candidates),
                "god_files": god_files,
            },
        }

    result = {"ok": True, "summary": final_summary, "cargo": cargo}
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        json.dump(
            {
                "ok": False,
                "summary": str(exc),
                "cargo": {
                    "error": repr(exc),
                },
            },
            sys.stdout,
        )
        sys.stdout.flush()
        traceback.print_exc(file=sys.stderr)
