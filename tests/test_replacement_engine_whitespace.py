from __future__ import annotations

from typing import Any

from aura.conversation.tools._replacement_engine import apply_replacement_to_content
from aura.conversation.tools.fs_write import propose_edit


def _assert_no_carriage_returns(value: Any) -> None:
    if isinstance(value, str):
        assert "\r" not in value
    elif isinstance(value, dict):
        for item in value.values():
            _assert_no_carriage_returns(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_carriage_returns(item)


def test_exact_lf_hunk_preserves_crlf_when_collapsing_blank_line() -> None:
    content = "a()\r\n\r\n\r\nb()\r\n"

    result = apply_replacement_to_content(
        content,
        "a()\n\n\nb()",
        "a()\n\nb()",
    )

    assert result["ok"] is True
    assert result["match_tier"] == "exact"
    assert result["content"] == "a()\r\n\r\nb()\r\n"
    assert "\r\n" in result["content"]
    assert "\n" not in result["content"].replace("\r\n", "")


def test_not_found_candidates_render_blank_line_trailing_spaces() -> None:
    content = "a()\n    \n\nb()\n"

    result = apply_replacement_to_content(
        content,
        "a()\n\n\nb()",
        "a()\n\nb()",
    )

    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert "    " in result["nearest_candidates"][0]["text"]
    assert "'    '" in result["nearest_candidates"][0]["text"]
    assert "\r" not in result["nearest_candidates"][0]["text"]


def test_wrong_blank_line_count_fails_without_joining_statements() -> None:
    content = "before()\nfirst()\n\n\nsecond()\nafter()\n"

    result = apply_replacement_to_content(
        content,
        "first()\n\nsecond()",
        "first()\nupdated()",
    )

    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert "content" not in result


def test_indentation_only_fuzzy_match_is_diagnostic_only() -> None:
    content = "if ready:\n    call()\n"

    result = apply_replacement_to_content(
        content,
        "if ready:\ncall()",
        "if ready:\n    call_updated()",
    )

    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert result["best_fuzzy_ratio"] == 1.0
    assert "content" not in result


def test_line_exact_without_old_trailing_newline_preserves_following_line() -> None:
    content = "x = 1\nz = 3\nx = 10\n"

    result = apply_replacement_to_content(
        content,
        "x = 1",
        "y = 2",
    )

    assert result["ok"] is True
    assert result["match_tier"] == "line_exact"
    assert result["content"] == "y = 2\nz = 3\nx = 10\n"


def test_line_exact_with_old_trailing_newline_consumes_terminator() -> None:
    content = "prefix x = 1\nx = 1\nnext()\n"

    result = apply_replacement_to_content(
        content,
        "x = 1\n",
        "y = 2\n",
    )

    assert result["ok"] is True
    assert result["match_tier"] == "line_exact"
    assert result["content"] == "prefix x = 1\ny = 2\nnext()\n"


def test_exact_unique_and_duplicate_ambiguity_are_unchanged() -> None:
    unique = apply_replacement_to_content("a()\nb()\n", "a()", "c()")
    assert unique["ok"] is True
    assert unique["match_tier"] == "exact"
    assert unique["content"] == "c()\nb()\n"

    exact_duplicate = apply_replacement_to_content(
        "a()\nb()\na()\nb()\n",
        "a()\nb()",
        "c()",
        exact_duplicates_are_ambiguous=True,
    )
    assert exact_duplicate["ok"] is False
    assert exact_duplicate["reason"] == "ambiguous"
    assert exact_duplicate["match_tier"] == "exact"

    line_duplicate = apply_replacement_to_content(
        "x = 1\nx = 1\n",
        "x = 1\r",
        "y = 2",
        sanitize=False,
        exact_duplicates_are_ambiguous=True,
    )
    assert line_duplicate["ok"] is False
    assert line_duplicate["reason"] == "ambiguous"
    assert line_duplicate["match_tier"] == "line_exact"


def test_propose_edit_reports_duplicate_ambiguity(tmp_path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8", newline="\n")

    result = propose_edit(tmp_path, target, "alpha\nbeta", "gamma")

    assert result["ok"] is False
    assert result["failure_class"] == "edit_mechanics_ambiguous_match"
    assert result["match_tier"] == "exact"


def test_propose_edit_uses_raw_pass_before_sanitizing(tmp_path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("before\n  \nafter\n", encoding="utf-8", newline="\n")

    result = propose_edit(tmp_path, target, "\n  \n", "\n")

    assert result["ok"] is True
    assert result["match_tier"] == "exact"
    assert result["new_content"] == "before\nafter\n"


def test_failure_payload_contains_no_carriage_returns() -> None:
    result = apply_replacement_to_content(
        "a()\r\n    \r\nb()\r\n",
        "missing\r\nblock",
        "replacement",
    )

    assert result["ok"] is False
    _assert_no_carriage_returns(result)
