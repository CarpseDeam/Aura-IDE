from __future__ import annotations

from aura.humanizer.features import (
    CodeFeatureReport,
    GenericNameHit,
    NarrationCommentHit,
    ThinHelperHit,
    TupleReturnHit,
    analyze_python_features,
)
from aura.humanizer.pipeline import HumanizerPipeline


class TestLargeTupleReturns:
    def test_detects_large_tuple_return(self):
        code = """def get_stats():
    return (1, 2, 3, 4)
"""
        report = analyze_python_features(code)
        assert len(report.tuple_returns) == 1
        hit = report.tuple_returns[0]
        assert hit.function_name == "get_stats"
        assert hit.size == 4
        assert hit.line == 2

    def test_ignores_small_tuple_return(self):
        code = """def get_pair():
    return (1, 2)
"""
        report = analyze_python_features(code)
        assert len(report.tuple_returns) == 0

    def test_detects_async_function(self):
        code = """async def fetch_data():
    return (1, 2, 3, 4, 5)
"""
        report = analyze_python_features(code)
        assert len(report.tuple_returns) == 1
        assert report.tuple_returns[0].function_name == "fetch_data"
        assert report.tuple_returns[0].size == 5


class TestGenericNames:
    def test_detects_generic_assignments(self):
        code = """data = []
result = {}
item = None
items = []
values = []
"""
        report = analyze_python_features(code)
        names = {(h.name, h.line) for h in report.generic_names}
        assert ("data", 1) in names
        assert ("result", 2) in names
        assert ("item", 3) in names
        assert ("items", 4) in names
        assert ("values", 5) in names

    def test_detects_generic_function_args(self):
        code = """def process(data, result, items):
    pass
"""
        report = analyze_python_features(code)
        names = {(h.name, h.line) for h in report.generic_names}
        assert ("data", 1) in names
        assert ("result", 1) in names
        assert ("items", 1) in names

    def test_deduplicates_same_name_on_same_line(self):
        code = """result = result
"""
        report = analyze_python_features(code)
        # "result" appears twice on line 1 but should be reported once
        hits = [h for h in report.generic_names if h.name == "result" and h.line == 1]
        assert len(hits) == 1

    def test_ignores_non_generic_names(self):
        code = """username = "alice"
user_count = 42
items_list = []
"""
        report = analyze_python_features(code)
        assert len(report.generic_names) == 0


class TestNarrationComments:
    def test_detects_narration_comments(self):
        code = """# Initialize the counter
count = 0
# Loop through items
for item in items:
    # Process each element
    print(item)
"""
        report = analyze_python_features(code)
        assert len(report.narration_comments) == 3
        texts = {nc.text for nc in report.narration_comments}
        assert "# Initialize the counter" in texts
        assert "# Loop through items" in texts
        assert "# Process each element" in texts
    def test_skips_excluded_comments(self):
        code = """# TODO: refactor this
x = 1  # noqa
# See https://example.com for details
# Copyright 2024 Acme Corp
y = 2  # type: ignore
"""
        report = analyze_python_features(code)
        assert len(report.narration_comments) == 0


class TestThinHelpers:
    def test_detects_thin_private_helper(self):
        code = """def _helper():
    return 42
"""
        report = analyze_python_features(code)
        assert len(report.thin_helpers) == 1
        hit = report.thin_helpers[0]
        assert hit.function_name == "_helper"
        assert hit.body_lines <= 3

    def test_ignores_non_private_function(self):
        code = """def helper():
    return 42
"""
        report = analyze_python_features(code)
        assert len(report.thin_helpers) == 0

    def test_ignores_dunder_method(self):
        code = """class Foo:
    def __str__(self):
        return "Foo"
"""
        report = analyze_python_features(code)
        assert len(report.thin_helpers) == 0

    def test_ignores_function_with_many_lines(self):
        code = """def _process():
    x = 1
    y = 2
    z = 3
    w = 4
    return w
"""
        report = analyze_python_features(code)
        assert len(report.thin_helpers) == 0


class TestCodeFeatureReport:
    def test_empty_report_has_no_smells(self):
        report = CodeFeatureReport()
        assert report.has_structural_smells is False

    def test_tuple_returns_triggers_smell(self):
        report = CodeFeatureReport(tuple_returns=[TupleReturnHit("fn", 1, 4)])
        assert report.has_structural_smells is True

    def test_generic_names_triggers_smell(self):
        report = CodeFeatureReport(generic_names=[GenericNameHit("data", 1)])
        assert report.has_structural_smells is True

    def test_narration_comments_triggers_smell(self):
        report = CodeFeatureReport(
            narration_comments=[NarrationCommentHit("Initialize x", 1)]
        )
        assert report.has_structural_smells is True

    def test_thin_helpers_triggers_smell(self):
        report = CodeFeatureReport(thin_helpers=[ThinHelperHit("_h", 1, 1)])
        assert report.has_structural_smells is True


class TestSyntaxError:
    def test_returns_empty_report_on_syntax_error(self):
        code = "this is @@ invalid python"
        report = analyze_python_features(code)
        assert isinstance(report, CodeFeatureReport)
        assert len(report.tuple_returns) == 0
        assert len(report.generic_names) == 0
        assert len(report.narration_comments) == 0
        assert len(report.thin_helpers) == 0
        assert report.has_structural_smells is False


class TestIntegration:
    def test_detects_multiple_patterns(self):
        code = """# Initialize result list
def get_values():
    result = []
    return (1, 2, 3, 4)


def _helper():
    return 42
"""
        report = analyze_python_features(code)
        assert len(report.tuple_returns) == 1
        assert report.tuple_returns[0].function_name == "get_values"
        assert report.tuple_returns[0].size == 4
        assert len(report.generic_names) >= 1  # "result"
        assert len(report.narration_comments) == 1
        assert report.narration_comments[0].text == "# Initialize result list"
        assert len(report.thin_helpers) == 1
        assert report.thin_helpers[0].function_name == "_helper"
        assert report.has_structural_smells is True

    def test_analyzer_does_not_mutate_source(self):
        code = "x = 1\ny = 2\n"
        original = code
        analyze_python_features(code)
        assert code == original

    def test_pipeline_includes_feature_report(self):
        code = "def scan(root):\n    return files, dupes, hashes, skipped, errors\n"
        result = HumanizerPipeline().humanize_code(code, language="python")
        assert result.feature_report is not None
        assert result.structural_smell_count > 0
        assert len(result.feature_report.tuple_returns) == 1

    def test_pipeline_keeps_original_on_invalid_python(self):
        code = "this is not valid python @@"
        result = HumanizerPipeline().humanize_code(code, language="python")
        assert result.error is not None
        assert result.text == code
        assert result.feature_report is None
