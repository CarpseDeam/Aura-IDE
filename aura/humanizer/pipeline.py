from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path

from aura.humanizer.comments import remove_ai_filler_comments
from aura.humanizer.docstrings import remove_internal_docstrings
from aura.humanizer.features import analyze_python_features
from aura.humanizer.markdown import strip_markdown_wrapper
from aura.humanizer.result import HumanizerResult

_log = logging.getLogger("aura.humanizer")


def is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


class HumanizerPipeline:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root

    def humanize_code(
        self,
        code: str,
        language: str = "python",
        path: Path | None = None,
    ) -> HumanizerResult:
        start = time.perf_counter()
        result = HumanizerResult(path=path, language=language, original=code, text=code)

        try:
            if language != "python":
                result.text = code
                result.elapsed_ms = (time.perf_counter() - start) * 1000
                return result

            current = code

            # Step 1: Strip markdown wrapper (before validation so fenced code works)
            stripped, md_stripped = strip_markdown_wrapper(current)
            current = stripped
            result.markdown_stripped = md_stripped

            # Step 2: Check if the code (after markdown strip) is valid Python
            if not is_valid_python(current):
                result.text = code
                result.syntax_fallback = True
                result.error = "Source code is not valid Python"
                result.elapsed_ms = (time.perf_counter() - start) * 1000
                return result

            # Step 2.5: Run feature analysis (read-only, never fails)
            try:
                report = analyze_python_features(current)
                result.feature_report = report
                result.structural_smell_count = (
                    len(report.tuple_returns)
                    + len(report.generic_names)
                    + len(report.narration_comments)
                    + len(report.thin_helpers)
                )
            except Exception:
                pass  # feature analysis is optional; never block the pipeline

            # Step 3: Remove AI filler comments
            cleaned, comments_removed = remove_ai_filler_comments(current)
            current = cleaned
            result.comments_removed = comments_removed

            # Step 4: Remove internal docstrings (cheap pre-check)
            if '"""' in current or "'''" in current:
                cleaned_ds, docstrings_removed = remove_internal_docstrings(current)
                current = cleaned_ds
                result.docstrings_removed = docstrings_removed

            # Verify transformed code still parses
            if not is_valid_python(current):
                result.text = code
                result.syntax_fallback = True
                result.error = "Transformed code failed syntax check"
                result.elapsed_ms = (time.perf_counter() - start) * 1000
                return result

            result.text = current
            result.changed = md_stripped or comments_removed > 0 or docstrings_removed > 0
            result.elapsed_ms = (time.perf_counter() - start) * 1000

            if os.environ.get("AURA_HUMANIZER_FEATURE_LOG") == "1" and result.feature_report is not None:
                r = result.feature_report
                if r.has_structural_smells:
                    path_str = str(path) if path else "<unknown>"
                    _log.info(
                        "[humanizer:features] %s: %d tuple returns, %d generic names, %d narration comments, %d thin helpers",
                        path_str, len(r.tuple_returns), len(r.generic_names),
                        len(r.narration_comments), len(r.thin_helpers),
                    )
                    for t in r.tuple_returns:
                        _log.info("[humanizer:features] %s: %s returns %d values on line %d",
                                  path_str, t.function_name, t.size, t.line)

            return result

        except Exception as exc:
            result.text = code
            result.error = str(exc)
            result.elapsed_ms = (time.perf_counter() - start) * 1000
            return result
