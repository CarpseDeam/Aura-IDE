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
