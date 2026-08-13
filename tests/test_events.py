from aura.events import ALL, EXECUTION_TOOL_STARTED, AuraEvent, EventBus


def test_event_bus_delivers_execution_facts_to_topic_and_wildcard_subscribers() -> None:
    bus = EventBus()
    direct: list[AuraEvent] = []
    all_events: list[AuraEvent] = []
    bus.subscribe(EXECUTION_TOOL_STARTED, direct.append)
    bus.subscribe(ALL, all_events.append)

    event = AuraEvent(
        topic=EXECUTION_TOOL_STARTED,
        run_id="prod-1",
        payload={"name": "read_file"},
    )
    bus.emit(event)

    assert direct == [event]
    assert all_events == [event]
    assert event.to_dict()["run_id"] == "prod-1"
    assert "artifact_id" not in event.to_dict()
