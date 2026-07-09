"""Tests for terminal-driven syntax state mutation (demoted: failure no longer creates state)."""

from __future__ import annotations

from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal


class TestUpdateSyntaxStateFromTerminal:
    """Exit-code == 0 clears tracked state; failure no longer creates new state."""

    def test_exit_zero_clears_tracked_path(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        syntax_repair_required = {"app.py": {"error": "old", "failed_repairs": 1}}
        syntax_validation_required = {"app.py"}
        update_syntax_state_from_terminal(
            args={},
            loop_info={
                "_terminal_payload": {
                    "command": "python -m py_compile app.py",
                    "exit_code": 0,
                    "output": "",
                }
            },
            workspace_root=tmp_path,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
        )
        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_exit_zero_no_prior_state_noop(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        update_syntax_state_from_terminal(
            args={},
            loop_info={
                "_terminal_payload": {
                    "command": "python -m py_compile app.py",
                    "exit_code": 0,
                    "output": "",
                }
            },
            workspace_root=tmp_path,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
        )
        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_exit_failure_no_longer_creates_state(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        update_syntax_state_from_terminal(
            args={},
            loop_info={
                "_terminal_payload": {
                    "command": "python -m py_compile app.py",
                    "exit_code": 1,
                    "output": 'File "app.py", line 1, SyntaxError: bad syntax',
                }
            },
            workspace_root=tmp_path,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
        )
        # Failure no longer creates repair state.
        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_exit_failure_does_not_clear_existing_state(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        syntax_repair_required = {"app.py": {"error": "old"}}
        syntax_validation_required: set[str] = set()
        update_syntax_state_from_terminal(
            args={},
            loop_info={
                "_terminal_payload": {
                    "command": "python -m py_compile app.py",
                    "exit_code": 1,
                    "output": 'File "app.py", line 1, SyntaxError: bad syntax',
                }
            },
            workspace_root=tmp_path,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
        )
        # Existing state is untouched by a failure exit.
        assert syntax_repair_required == {"app.py": {"error": "old"}}
        assert syntax_validation_required == set()

    def test_no_targets_noop(self, tmp_path):
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        update_syntax_state_from_terminal(
            args={},
            loop_info={
                "_terminal_payload": {
                    "command": "ruff check app.py",
                    "exit_code": 0,
                    "output": "",
                }
            },
            workspace_root=tmp_path,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
        )
        assert syntax_repair_required == {}
        assert syntax_validation_required == set()
