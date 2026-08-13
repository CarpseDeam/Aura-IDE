from aura.bridge.harness_lap_bridge import _RECEIPT_STATUS_TO_EXECUTION
from aura.bridge.production_receipt import STATUS_COMPLETED, STATUS_VALIDATION_FAILED
from aura.conversation.execution_outcome import ExecutionOutcomeStatus


def test_harness_laps_use_role_neutral_execution_outcomes() -> None:
    assert _RECEIPT_STATUS_TO_EXECUTION[STATUS_COMPLETED] == ExecutionOutcomeStatus.completed.value
    assert (
        _RECEIPT_STATUS_TO_EXECUTION[STATUS_VALIDATION_FAILED]
        == ExecutionOutcomeStatus.validation_failed.value
    )
