"""Built-in actions bypass the model, so a false positive drops real work."""
from aura.conversation.task_router import TaskLane, classify_user_request


def _action(text: str) -> str | None:
    route = classify_user_request(text)
    return route.action if route.lane == TaskLane.built_in_action else None


def test_long_prompt_mentioning_restore_and_snapshot_is_not_a_built_in():
    text = (
        "Work on the equipment transaction bug.\n"
        "If the tests break, restore the files to a clean state and re-run them.\n"
        "Then report back with:\n"
        "- equipment transaction behavior\n"
        "- a snapshot of focused tests and results\n"
        "- confirmation that nothing was pushed"
    )

    assert _action(text) is None


def test_long_prompt_mentioning_undo_and_commit_is_not_a_built_in():
    text = (
        "Fix the traversal regression, and if the last change made it worse "
        "undo it before you commit anything to the branch."
    )

    assert _action(text) is None


def test_prompt_mentioning_soft_reset_in_passing_is_not_a_built_in():
    text = (
        "Explain when a soft reset is safer than a hard reset, and update the "
        "contributor docs with the answer."
    )

    assert _action(text) is None


def test_explicit_commands_still_route_to_built_ins():
    assert _action("/undo") == "undo"
    assert _action("undo the last commit") == "undo"
    assert _action("please undo my most recent commit") == "undo"
    assert _action("git reset --soft HEAD~1") == "undo"
    assert _action("restore snapshot") == "restore_snapshot"
    assert _action("restore snapshot a1b2c3d") == "restore_snapshot"
    assert _action("can you restore to the snapshot") == "restore_snapshot"
    assert _action("git status") == "git_status"
    assert _action("please git diff") == "git_diff"
    assert _action("git log --oneline") == "git_log"
    assert _action("/drone") == "drone_enter_mode"
