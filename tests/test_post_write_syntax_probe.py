"""Tests for aura.conversation.syntax_probe_state.run_post_write_probe."""

from __future__ import annotations

from aura.conversation.syntax_probe_state import run_post_write_probe


class TestRunPostWriteProbe:
    """run_post_write_probe behaviour for various file states."""

    def test_valid_python_clears_repair_state(self, tmp_path) -> None:
        valid_py = tmp_path / "valid.py"
        valid_py.write_text("x = 1\n")

        syntax_repair_required = {
            "valid.py": {"error": "old", "failed_repairs": 1}
        }
        syntax_validation_required = {"valid.py"}

        run_post_write_probe(
            tmp_path,
            "valid.py",
            syntax_repair_required,
            syntax_validation_required,
        )

        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_invalid_python_sets_repair_state(self, tmp_path) -> None:
        broken_py = tmp_path / "broken.py"
        broken_py.write_text("x =\n")

        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()

        run_post_write_probe(
            tmp_path,
            "broken.py",
            syntax_repair_required,
            syntax_validation_required,
        )

        assert "broken.py" in syntax_repair_required
        assert (
            syntax_repair_required["broken.py"]["probe_evidence"] == "fail"
        )
        assert (
            syntax_repair_required["broken.py"]["failure_class"]
            == "syntax_invalid"
        )
        assert syntax_repair_required["broken.py"]["awaiting_validation"] is False
        assert "broken.py" not in syntax_validation_required

    def test_non_python_file_no_registered_probe(self, tmp_path) -> None:
        rs_file = tmp_path / "main.rs"
        rs_file.write_text("fn main() {}\n")

        syntax_repair_required: dict = {
            "main.rs": {"error": "old"}
        }
        syntax_validation_required: set[str] = set()

        run_post_write_probe(
            tmp_path,
            "main.rs",
            syntax_repair_required,
            syntax_validation_required,
        )

        # Probe skipped — no probe registered for .rs files
        assert syntax_repair_required == {"main.rs": {"error": "old"}}

    def test_validation_scratch_path_not_probed(self, tmp_path) -> None:
        scratch_py = tmp_path / "tmp_check.py"
        scratch_py.write_text("x = 1\n")

        syntax_repair_required: dict = {
            "tmp_check.py": {"error": "old"}
        }
        syntax_validation_required: set[str] = set()

        run_post_write_probe(
            tmp_path,
            "tmp_check.py",
            syntax_repair_required,
            syntax_validation_required,
        )

        # Probe skipped — validation scratch path
        assert syntax_repair_required == {"tmp_check.py": {"error": "old"}}

    def test_workspace_root_none_skips_probe(self, tmp_path) -> None:
        syntax_repair_required: dict = {
            "valid.py": {"error": "old"}
        }
        syntax_validation_required: set[str] = set()

        run_post_write_probe(
            None,
            "valid.py",
            syntax_repair_required,
            syntax_validation_required,
        )

        # No error, no state change
        assert syntax_repair_required == {"valid.py": {"error": "old"}}
