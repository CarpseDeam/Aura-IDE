from aura.conversation.planner_dispatch_gate import maybe_force_worker_dispatch
from aura.conversation.task_router import TaskLane, classify_user_request


LOCAL_TEST_REQUEST = (
    "Add focused regression tests for aura/conversation/planner_dispatch_gate.py."
)


def _assistant_message(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _dispatch_gate(
    assistant_text: str,
    *,
    latest_user_text: str = LOCAL_TEST_REQUEST,
    planner_tool_calls_seen: int = 0,
    dispatch_calls_seen: int = 0,
    already_steered: bool = False,
):
    return maybe_force_worker_dispatch(
        latest_user_text=latest_user_text,
        candidate_message=_assistant_message(assistant_text),
        planner_tool_calls_seen=planner_tool_calls_seen,
        dispatch_calls_seen=dispatch_calls_seen,
        already_steered=already_steered,
    )


def test_first_response_test_file_narration_forces_dispatch():
    decision = _dispatch_gate("Let me write the focused test files.")

    assert decision.should_continue is True
    assert "local implementation/test/refactor work" in decision.steering_message
    assert "Do not answer in chat." in decision.steering_message
    assert "one short inspection pass" in decision.steering_message
    assert "already inspected enough context" not in decision.steering_message


def test_post_inspection_test_file_narration_forces_dispatch():
    decision = _dispatch_gate(
        "Let me write the focused test files.",
        planner_tool_calls_seen=2,
    )

    assert decision.should_continue is True


def test_existing_dispatch_call_disables_dispatch_gate():
    decision = _dispatch_gate(
        "Let me write the focused test files.",
        dispatch_calls_seen=1,
    )

    assert decision.should_continue is False


def test_already_steered_disables_dispatch_gate():
    decision = _dispatch_gate(
        "Let me write the focused test files.",
        already_steered=True,
    )

    assert decision.should_continue is False


def test_ill_write_tests_narration_forces_dispatch():
    decision = _dispatch_gate(
        "Ill write the tests.",
        latest_user_text="Write tests for the planner dispatch gate.",
    )

    assert decision.should_continue is True


def test_now_ill_create_regression_tests_narration_forces_dispatch():
    decision = _dispatch_gate("Now Ill create the regression tests.")

    assert decision.should_continue is True


def test_real_user_owned_blocker_question_does_not_force_dispatch():
    decision = _dispatch_gate("Which test file should own this case?")

    assert decision.should_continue is False


def test_pure_research_request_does_not_force_dispatch():
    decision = _dispatch_gate(
        "Let me write the focused test files.",
        latest_user_text="What is the current Python release schedule?",
    )

    assert decision.should_continue is False


def test_validation_commands_stay_validation_not_implementation():
    for text in ("run pytest", "python -m pytest"):
        route = classify_user_request(text)

        assert route.lane == TaskLane.validation


def test_test_writing_requests_route_as_implementation():
    for text in (
        "write tests",
        "add tests",
        "create regression tests",
        "write focused test files",
    ):
        route = classify_user_request(text)

        assert route.lane == TaskLane.implementation
