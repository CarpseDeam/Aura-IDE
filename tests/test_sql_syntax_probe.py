"""Tests for SQLSyntaxProbe."""

from __future__ import annotations

from unittest.mock import patch

from aura.syntax_probe.sql_probe import SQLSyntaxProbe

VALID_SQL = "SELECT * FROM users WHERE id = 1;"
INVALID_SQL = "SELECT * FROM users WHERE"


class TestSQLSyntaxProbe:
    """Tests for SQLSyntaxProbe."""

    # --- detect ---

    def test_detect_sql_file(self) -> None:
        assert SQLSyntaxProbe.detect("query.sql") is True
        assert SQLSyntaxProbe.detect("sub/create_table.sql") is True
        assert SQLSyntaxProbe.detect("migration.sql") is True

    def test_detect_case_insensitive(self) -> None:
        assert SQLSyntaxProbe.detect("QUERY.SQL") is True
        assert SQLSyntaxProbe.detect("Query.Sql") is True

    def test_detect_non_sql_file(self) -> None:
        assert SQLSyntaxProbe.detect("index.html") is False
        assert SQLSyntaxProbe.detect("style.css") is False
        assert SQLSyntaxProbe.detect("data.json") is False

    # --- valid SQL -> pass ---

    def test_valid_sql_returns_pass(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        sql_file = tmp_path / "valid.sql"
        sql_file.write_text(VALID_SQL)
        with patch(
            "aura.syntax_probe.sql_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("pass", None, None, "")
            result = probe.check(tmp_path, "valid.sql")
        assert result.evidence == "pass"
        assert result.ok is True
        assert result.failed is False

    # --- invalid SQL -> fail ---

    def test_invalid_sql_returns_fail(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        sql_file = tmp_path / "invalid.sql"
        sql_file.write_text(INVALID_SQL)
        with patch(
            "aura.syntax_probe.sql_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 1, 5, "Syntax error at line 1")
            result = probe.check(tmp_path, "invalid.sql")
        assert result.evidence == "fail"
        assert result.failed is True
        assert result.ok is False
        assert result.line == 1
        assert result.column == 5
        assert "Syntax error" in result.error
        assert result.failure_class == "syntax_invalid"

    def test_invalid_sql_preserves_line_and_column(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        sql_file = tmp_path / "invalid2.sql"
        sql_file.write_text(INVALID_SQL)
        with patch(
            "aura.syntax_probe.sql_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = ("fail", 3, 10, "Syntax error at line 3")
            result = probe.check(tmp_path, "invalid2.sql")
        assert result.evidence == "fail"
        assert result.line == 3
        assert result.column == 10
        assert result.failure_class == "syntax_invalid"

    # --- missing file -> no_evidence ---

    def test_missing_file_returns_no_evidence(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        result = probe.check(tmp_path, "nonexistent.sql")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False

    # --- parse failure -> no_evidence ---

    def test_parse_failure_returns_no_evidence(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        sql_file = tmp_path / "parse_fail.sql"
        sql_file.write_text(VALID_SQL)
        with patch(
            "aura.syntax_probe.sql_probe._tree_sitter_check"
        ) as mock_check:
            mock_check.return_value = (
                "no_evidence",
                None,
                None,
                "Parse failed: bad bytes",
            )
            result = probe.check(tmp_path, "parse_fail.sql")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
        assert "failed" in result.error

    # --- outside-workspace safety ---

    def test_absolute_path_outside_workspace(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        outside_dir = tmp_path.parent / "_outside_tmp_sql"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "outside.sql"
        outside_file.write_text(VALID_SQL)
        try:
            result = probe.check(tmp_path, str(outside_file))
            assert result.evidence == "no_evidence"
            assert result.ok is False
            assert result.failed is False
        finally:
            outside_file.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_relative_path_escape(self, tmp_path) -> None:
        probe = SQLSyntaxProbe()
        result = probe.check(tmp_path, "../outside_workspace.sql")
        assert result.evidence == "no_evidence"
        assert result.ok is False
        assert result.failed is False
