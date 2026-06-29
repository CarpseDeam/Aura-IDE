from aura.conversation.manager_send_state import _SendState
from aura.conversation.worker_flow import WorkerFlowHarness
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer


def test_send_state_initializes_worker_only_fields_for_all_modes():
    planner = _SendState(mode="planner", research_policy=None)
    single = _SendState(mode="single", research_policy=None)
    worker = _SendState(mode="worker", research_policy=None)

    assert planner.stream_buffer is None
    assert planner.worker_flow is None
    assert single.stream_buffer is None
    assert single.worker_flow is None
    assert isinstance(worker.stream_buffer, WorkerStreamBuffer)
    assert isinstance(worker.worker_flow, WorkerFlowHarness)
