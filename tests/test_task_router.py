from aura.conversation.task_router import TaskLane, classify_user_request
from aura.research.policy import ANSWER_ONLY, decide_research_policy


def test_world_cup_schedule_question_routes_to_answer_only_research():
    text = "Are there any World Cup matches today?"

    route = classify_user_request(text)
    policy = decide_research_policy(text)

    assert route.lane == TaskLane.research
    assert route.action == "web_research"
    assert policy.route == ANSWER_ONLY
    assert policy.allow_worker_dispatch is False
    assert policy.requires_research_first is True


def test_implementation_lane_names_the_shape_of_the_work():
    """The implementation lane carries a shape the context system understands.

    Lane membership is unchanged — an existing implementation trigger verb is
    still what admits a request to the lane. The action only names the shape of
    the work once it is there.
    """
    cases = {
        "fix the crash in the loader": "bugfix",
        "repair the broken queue drainer": "bugfix",
        "refactor the send handler": "refactor",
        "clean up the unused imports": "cleanup",
        "add a retry button to the toolbar": "implementation",
    }
    for text, expected in cases.items():
        route = classify_user_request(text)
        assert route.lane == TaskLane.implementation, text
        assert route.action == expected, text


def test_bugfix_wins_over_refactor_language():
    """A request to fix refactored code is a bugfix, not a refactor."""
    assert classify_user_request("fix the refactored loader").action == "bugfix"
