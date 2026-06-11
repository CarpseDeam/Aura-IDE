"""Tests for aura.drones.build_compiler — deterministic Drone build planning."""

from __future__ import annotations

from aura.drones.build_compiler import (
    DroneBuildPlan,
    compile_drone_build_plan,
    CORE_INTENT_MAP,
)
from aura.drones.definition import default_tools_for_policy


# ---------------------------------------------------------------------------
# Helper: the full harness tool surface plus drone ops tools.
# This represents every real tool name the system knows about, not just the
# default Drone tool set.
# ---------------------------------------------------------------------------
_DRONE_OPS_TOOLS = (
    "launch_read_only_drone",
    "run_read_only_drone",
    "check_drone_run",
    "summon_drone",
    "save_drone_definition",
)


def _harness_tools() -> frozenset[str]:
    return frozenset(
        default_tools_for_policy("normal_diff_approval") + _DRONE_OPS_TOOLS
    )


# ===================================================================
# TestCompileDroneBuildPlan
# ===================================================================


class TestCompileDroneBuildPlan:
    """Deterministic build plan compilation from brief text."""

    def test_git_commit_push_compiles_to_harness_tools(self):
        """Git Commit & Push brief -> git_status, git_diff, run_terminal_command."""
        brief = "Automatically commit and push changes to the remote repository."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "git_status" in plan.allowed_tools
        assert "git_diff" in plan.allowed_tools
        assert "run_terminal_command" in plan.allowed_tools
        assert len(plan.capability_requirements) == 0
        assert plan.generated_code_allowed is False

    def test_git_commit_push_does_not_produce_generated_code(self):
        """Git Commit & Push must not set generated_code_allowed."""
        brief = "Commit staged changes, push to origin, report the result."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert plan.generated_code_allowed is False

    def test_file_search_drone_compiles_to_read_search_tools(self):
        """File search/edit Drone -> read/search/edit tools."""
        brief = "Search code for relevant functions and read file implementations."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "grep_search" in plan.allowed_tools or "search_codebase" in plan.allowed_tools
        assert "read_file" in plan.allowed_tools
        assert len(plan.capability_requirements) == 0

    def test_external_email_brief_requires_capability_resolution(self):
        """Email brief -> capability_requirements, not random harness tools."""
        brief = "Send an email notification when the build fails."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert len(plan.capability_requirements) > 0
        assert "git_status" not in plan.allowed_tools
        assert plan.generated_code_allowed is False

    def test_external_browser_brief_requires_capability_resolution(self):
        """Browser brief -> capability_requirements."""
        brief = "Open Chrome and scrape the latest docs page."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert len(plan.capability_requirements) > 0
        assert plan.generated_code_allowed is False

    def test_external_database_brief_requires_capability_resolution(self):
        """Database brief -> capability_requirements."""
        brief = "Query the PostgreSQL database for user counts."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert len(plan.capability_requirements) > 0
        assert plan.generated_code_allowed is False

    def test_external_api_brief_requires_capability_resolution(self):
        """External API brief -> capability_requirements."""
        brief = "Call the GitHub REST API to list open pull requests."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert len(plan.capability_requirements) > 0
        assert plan.generated_code_allowed is False

    def test_generated_code_only_allowed_when_explicitly_requested(self):
        """Generated code flag is only True when brief explicitly asks for new tool."""
        brief = "Create a new tool to upload files to S3."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert plan.generated_code_allowed is True

    def test_generated_code_not_allowed_for_ordinary_wording(self):
        """Ordinary wording like 'handle file uploads' should NOT enable generated code."""
        brief = "Handle file uploads and manage the project configuration."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert plan.generated_code_allowed is False

    def test_ambiguous_brief_produces_warning(self):
        """A brief that matches nothing should produce a warning."""
        brief = "Do something mysterious with the foobar system."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert len(plan.warnings) > 0
        assert len(plan.allowed_tools) == 0
        assert plan.generated_code_allowed is False

    def test_empty_brief_produces_no_warnings(self):
        """Empty brief should not produce spurious warnings."""
        plan = compile_drone_build_plan("", _harness_tools())
        assert len(plan.warnings) == 0

    def test_workspace_snapshot_intent_maps_to_snapshot_tool(self):
        """Workspace snapshot phrase -> get_workspace_snapshot."""
        brief = "Get a workspace snapshot first, then check git status."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "get_workspace_snapshot" in plan.allowed_tools
        assert "git_status" in plan.allowed_tools

    def test_edit_write_intent_maps_to_edit_tools(self):
        """Edit/write phrase -> edit tools."""
        brief = "Edit files to fix bugs and write file changes."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "write_file" in plan.allowed_tools
        assert any(t in plan.allowed_tools for t in ("edit_file", "patch_file"))

    def test_terminal_command_intent(self):
        """Run command phrase -> run_terminal_command."""
        brief = "Run command and report build results."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "run_terminal_command" in plan.allowed_tools

    def test_compile_check_intent(self):
        """Compile check phrase -> run_diagnostic_command."""
        brief = "Run a compile check after editing Python files."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "run_diagnostic_command" in plan.allowed_tools

    def test_no_false_positive_harness_match_for_external(self):
        """External capabilities should not get harness tool matches."""
        brief = "Send an email and query the database."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "read_file" not in plan.allowed_tools

    def test_mixed_harness_and_external(self):
        """Mixed brief: harness tools for git, requirements for external."""
        brief = "Check git status, then send an email notification."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "git_status" in plan.allowed_tools
        assert len(plan.capability_requirements) > 0  # email

    def test_tools_filtered_to_available(self):
        """Tools not in available_tools should not appear in allowed_tools."""
        brief = "Read files, commit and push changes."
        restricted = frozenset(["read_file", "git_status"])
        plan = compile_drone_build_plan(brief, restricted)
        assert "read_file" in plan.allowed_tools
        assert "git_status" in plan.allowed_tools
        assert "run_terminal_command" not in plan.allowed_tools

    def test_drone_ops_intent_maps_to_drone_tools(self):
        """Drone ops phrase -> launch_read_only_drone etc."""
        brief = "Launch drones, check drone status, and save drone definition."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "launch_read_only_drone" in plan.allowed_tools
        assert "save_drone_definition" in plan.allowed_tools

    def test_read_files_intent_maps_to_read_files(self):
        brief = "Read files from the project directory."
        plan = compile_drone_build_plan(brief, _harness_tools())
        assert "read_files" in plan.allowed_tools


# ===================================================================
# TestCoreIntentMap
# ===================================================================


class TestCoreIntentMap:
    """Verify the CORE_INTENT_MAP is well-formed."""

    def test_all_tools_in_map_are_strings(self):
        for keyword, tools in CORE_INTENT_MAP:
            assert isinstance(keyword, str)
            for t in tools:
                assert isinstance(t, str)

    def test_all_tools_in_map_exist_in_harness(self):
        """Every tool name in CORE_INTENT_MAP should be a real system tool."""
        harness = _harness_tools()
        for keyword, tools in CORE_INTENT_MAP:
            for t in tools:
                assert t in harness, (
                    f"Tool '{t}' from intent '{keyword}' not in system tools"
                )

    def test_git_intents_present(self):
        """Verify git intents exist (not a Git-only hack but general coverage)."""
        intents = {kw for kw, _ in CORE_INTENT_MAP}
        assert "commit" in intents
        assert "push" in intents
        assert "git status" in intents
        assert "git diff" in intents

    def test_file_intents_present(self):
        intents = {kw for kw, _ in CORE_INTENT_MAP}
        assert "read file" in intents
        assert "search code" in intents
        assert "write file" in intents

    def test_workspace_intents_present(self):
        intents = {kw for kw, _ in CORE_INTENT_MAP}
        assert "workspace snapshot" in intents

    def test_drone_ops_intents_present(self):
        intents = {kw for kw, _ in CORE_INTENT_MAP}
        assert "launch drone" in intents
        assert "save drone" in intents
        assert "check drone" in intents
