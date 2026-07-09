"""Tests for the shared tree-sitter syntax-check utility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aura.syntax_probe.tree_sitter_utils import _tree_sitter_check


def _make_mock_node(
    node_type: str,
    row: int = 0,
    column: int = 0,
    children: list | None = None,
) -> MagicMock:
    """Build a mock tree-sitter node."""
    node = MagicMock()
    node.type = node_type
    pt = MagicMock()
    pt.row = row
    pt.column = column
    node.start_point = pt
    node.children = children or []
    return node


class TestTreeSitterCheck:
    """Tests for _tree_sitter_check."""

    # ── pass ────────────────────────────────────────────────────────────────

    def test_pass_valid_file(self, tmp_path) -> None:
        """A syntactically valid file returns ('pass', None, None, '')."""
        source = tmp_path / "valid.html"
        source.write_text("<html><body><p>ok</p></body></html>")

        root = _make_mock_node("document", children=[])
        tree = MagicMock()
        tree.root_node = root

        parser = MagicMock()
        parser.parse.return_value = tree

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "html")

        assert evidence == "pass"
        assert line is None
        assert column is None
        assert msg == ""

    # ── fail ────────────────────────────────────────────────────────────────

    def test_fail_error_node(self, tmp_path) -> None:
        """An ERROR node causes ('fail', line, col, message)."""
        source = tmp_path / "bad.html"
        source.write_text("<html><body><p>broken")

        error_node = _make_mock_node("ERROR", row=0, column=5)
        root = _make_mock_node("document", children=[error_node])
        tree = MagicMock()
        tree.root_node = root

        parser = MagicMock()
        parser.parse.return_value = tree

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "html")

        assert evidence == "fail"
        assert line == 1  # 1-based
        assert column == 5
        assert "Syntax error at line 1" in msg

    def test_fail_missing_node(self, tmp_path) -> None:
        """A MISSING node causes ('fail', line, col, message)."""
        source = tmp_path / "bad.css"
        source.write_text("body {")

        missing_node = _make_mock_node("MISSING", row=0, column=6)
        root = _make_mock_node("stylesheet", children=[missing_node])
        tree = MagicMock()
        tree.root_node = root

        parser = MagicMock()
        parser.parse.return_value = tree

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "css")

        assert evidence == "fail"
        assert line == 1
        assert column == 6
        assert "Syntax error at line 1" in msg

    def test_fail_deep_error_node(self, tmp_path) -> None:
        """First error deeper in the tree is still detected."""
        source = tmp_path / "nested.html"
        source.write_text("<div><span><p>")

        leaf = _make_mock_node("ERROR", row=1, column=3)
        inner = _make_mock_node("element", children=[leaf])
        outer = _make_mock_node("element", children=[inner])
        root = _make_mock_node("document", children=[outer])

        tree = MagicMock()
        tree.root_node = root

        parser = MagicMock()
        parser.parse.return_value = tree

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "html")

        assert evidence == "fail"
        assert line == 2  # 0-based row 1 → 1-based line 2
        assert column == 3
        assert "Syntax error at line 2" in msg

    # ── no_evidence ────────────────────────────────────────────────────────

    def test_no_evidence_get_language_raises(self, tmp_path) -> None:
        """If get_language raises, result is no_evidence."""
        source = tmp_path / "test.sql"
        source.write_text("SELECT * FROM t;")

        with patch(
            "tree_sitter_language_pack.get_language",
            side_effect=ValueError("unsupported"),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "sql")

        assert evidence == "no_evidence"
        assert line is None
        assert column is None
        assert "not available" in msg

    def test_no_evidence_get_language_returns_none(self, tmp_path) -> None:
        """If get_language returns None, result is no_evidence."""
        source = tmp_path / "test.sql"
        source.write_text("SELECT * FROM t;")

        with patch(
            "tree_sitter_language_pack.get_language",
            return_value=None,
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "sql")

        assert evidence == "no_evidence"
        assert line is None
        assert column is None
        assert "not found" in msg

    def test_no_evidence_parse_fails(self, tmp_path) -> None:
        """If parser.parse fails, result is no_evidence."""
        source = tmp_path / "test.html"
        source.write_text("<p>hi</p>")

        parser = MagicMock()
        parser.parse.side_effect = RuntimeError("bad parse")

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "html")

        assert evidence == "no_evidence"
        assert line is None
        assert column is None
        assert "failed" in msg

    def test_no_evidence_parse_returns_none(self, tmp_path) -> None:
        """If parser.parse returns None, result is no_evidence."""
        source = tmp_path / "test.html"
        source.write_text("<p>hi</p>")

        parser = MagicMock()
        parser.parse.return_value = None

        with (
            patch("tree_sitter.Parser", return_value=parser),
            patch(
                "tree_sitter_language_pack.get_language",
                return_value=MagicMock(),
            ),
        ):
            evidence, line, column, msg = _tree_sitter_check(source, "html")

        assert evidence == "no_evidence"
        assert line is None
        assert column is None
        assert "no tree" in msg

    def test_no_evidence_file_not_found(self, tmp_path) -> None:
        """Missing file returns no_evidence."""
        missing = tmp_path / "ghost.html"
        evidence, line, column, msg = _tree_sitter_check(missing, "html")

        assert evidence == "no_evidence"
        assert line is None
        assert column is None
        assert "not found" in msg
