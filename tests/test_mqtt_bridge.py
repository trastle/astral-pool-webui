import asyncio
import json
from unittest.mock import MagicMock, patch

from mqtt_bridge import MqttBridge, build_state_payload


def success_reason_code():
    return MagicMock(is_failure=False)


def failure_reason_code():
    return MagicMock(is_failure=True)


def test_build_state_payload_uses_real_enum_values(sample_data):
    """mode/pump_speed/chlorine_control_status must be raw ints (not our own
    humanized strings) - the astralpool_chlorinator fork's coordinator
    reconstructs the real pychlorinator enum via EnumClass(value)."""
    payload = build_state_payload(sample_data)
    assert payload["mode"] == 2
    assert payload["pump_speed"] == 1
    assert payload["chlorine_control_status"] == 0


def test_build_state_payload_includes_fields_the_fork_needs(sample_data):
    payload = build_state_payload(sample_data)
    for field in (
        "mode", "pump_speed", "active_timer", "info_message", "ph_measurement",
        "chlorine_control_status", "chemistry_values_current", "chemistry_values_valid",
        "time_hours", "time_minutes", "time_seconds", "spa_selection",
        "pump_is_priming", "pump_is_operating", "cell_is_operating",
        "sanitising_until_next_timer_tomorrow",
    ):
        assert field in payload, f"missing field the fork's entities read: {field}"


def test_build_state_payload_includes_extended_fields(sample_data):
    payload = build_state_payload(sample_data)
    assert payload["pool_volume_litres"] == 38000  # decoded from raw bytes
    assert payload["cell_running_time_days"] == 238.0
    assert payload["low_salt_cell_running_time_days"] == 33.5
    assert payload["acid_dosing_inhibit_status"] == "InhibitedIndefinitely"
    assert payload["default_manual_on_speed"] == 2
    assert payload["ph_control_type"] == "Automatic"
    assert payload["chlorine_control_type"] == "Automatic"
    assert payload["minimum_orp_setpoint"] == 200
    assert payload["maximum_orp_setpoint"] == 800
    assert payload["highest_ph_measured"] == 9.4
    assert payload["lowest_orp_measured"] == 100


def test_build_state_payload_is_json_serializable(sample_data):
    json.dumps(build_state_payload(sample_data))


def test_bridge_disabled_when_no_broker_configured(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", None)
    bridge = MqttBridge()
    assert bridge.enabled is False
    bridge.connect()
    bridge.publish_state({"mode": "Auto"})
    bridge.disconnect()


def test_bridge_disabled_when_explicitly_disabled_even_with_host_configured(monkeypatch):
    """mqtt.enabled=false should win even if a host/port/credentials are
    still configured - the whole point is disabling without losing them."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.MQTT_ENABLED", False)

    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        assert bridge.enabled is False
        bridge.connect()

    fake_client.connect_async.assert_not_called()


def test_bridge_enabled_by_default_when_host_configured(monkeypatch):
    """mqtt.enabled defaults to true - only MQTT_HOST determines whether
    the bridge is enabled unless something explicitly overrides it."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.MQTT_ENABLED", True)
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        assert MqttBridge().enabled is True


def test_bridge_connects_and_publishes_when_broker_configured(monkeypatch, sample_data):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.MQTT_USERNAME", None)

    fake_client = MagicMock()

    async def run():
        with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
            bridge = MqttBridge()
            assert bridge.enabled is True

            # connect() calls asyncio.get_running_loop() (to dispatch
            # commands later), so this needs a real running loop.
            bridge.connect()
            # connect_async(), not connect() - see the module docstring on
            # why the blocking call would defeat retrying a bad first
            # attempt. Actual subscribing happens in on_connect, invoked
            # below as paho itself would once a CONNACK arrives.
            fake_client.connect_async.assert_called_once_with("test-broker", 1883)
            fake_client.loop_start.assert_called_once()
            fake_client.subscribe.assert_not_called()
            assert bridge.connected is False

            bridge._on_connect(fake_client, None, MagicMock(), success_reason_code())
            fake_client.subscribe.assert_called_once_with(
                [("chlorinator/testpool/action", 0), ("chlorinator/testpool/setup", 0)]
            )
            assert bridge.connected is True
            assert bridge.last_connected_at is not None
            # No discovery publish anymore - just the connection itself.
            fake_client.publish.assert_not_called()

            bridge.publish_state(sample_data)
            fake_client.publish.assert_called_once()
            topic, payload_json = fake_client.publish.call_args[0]
            assert topic == "chlorinator/testpool/state"
            assert json.loads(payload_json)["mode"] == 2

            bridge.disconnect()
            fake_client.loop_stop.assert_called_once()
            fake_client.disconnect.assert_called_once()

    asyncio.run(run())


def test_bridge_connect_failure_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "unreachable-broker")

    fake_client = MagicMock()
    fake_client.connect_async.side_effect = OSError("connection refused")

    async def run():
        with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
            bridge = MqttBridge()
            bridge.connect()  # must not raise - app should keep running without MQTT

    asyncio.run(run())


def test_on_connect_resubscribes_on_a_reconnect_not_just_the_first_time(monkeypatch):
    """The whole point of subscribing from on_connect rather than once in
    connect(): a broker with clean sessions (the default) forgets
    subscriptions across a drop, so every reconnect needs to redo it too."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge._on_connect(fake_client, None, MagicMock(), success_reason_code())
        bridge._on_disconnect(fake_client, None, MagicMock(), MagicMock())
        bridge._on_connect(fake_client, None, MagicMock(), success_reason_code())

    assert fake_client.subscribe.call_count == 2
    assert bridge.connected is True


def test_on_connect_with_failure_reason_code_does_not_subscribe_or_mark_connected(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge._on_connect(fake_client, None, MagicMock(), failure_reason_code())

    fake_client.subscribe.assert_not_called()
    assert bridge.connected is False
    assert bridge.last_connected_at is None


def test_on_disconnect_marks_disconnected_and_counts_it(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge._on_connect(fake_client, None, MagicMock(), success_reason_code())
        assert bridge.disconnect_count == 0

        bridge._on_disconnect(fake_client, None, MagicMock(), MagicMock())
        assert bridge.connected is False
        assert bridge.disconnect_count == 1

        bridge._on_disconnect(fake_client, None, MagicMock(), MagicMock())
        assert bridge.disconnect_count == 2


def test_on_message_dispatches_action_and_setup_by_topic_suffix(monkeypatch):
    """_on_message runs on paho's own thread in production; here we call it
    directly (as if paho had) and check it hands off to the asyncio loop
    with the right (kind, payload)."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")

    received = []

    async def fake_handler(kind, payload):
        received.append((kind, payload))

    async def run():
        with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
            bridge = MqttBridge()
            bridge.set_command_handler(fake_handler)
            bridge.connect()  # sets self._loop

            action_msg = MagicMock(topic="chlorinator/testpool/action", payload=b'{"action": 2}')
            bridge._on_message(None, None, action_msg)
            setup_msg = MagicMock(topic="chlorinator/testpool/setup", payload=b'{"ph_control_setpoint": 7.4}')
            bridge._on_message(None, None, setup_msg)

            # run_coroutine_threadsafe schedules on the loop - let it run.
            await asyncio.sleep(0.05)

    asyncio.run(run())

    assert ("action", {"action": 2}) in received
    assert ("setup", {"ph_control_setpoint": 7.4}) in received


def test_on_message_ignores_malformed_payload_without_raising():
    async def fake_handler(kind, payload):
        raise AssertionError("should never be called with bad JSON")

    async def run():
        with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
            bridge = MqttBridge()
            bridge.set_command_handler(fake_handler)
            bridge._loop = asyncio.get_running_loop()

            bad_msg = MagicMock(topic="chlorinator/testpool/action", payload=b"not json")
            bridge._on_message(None, None, bad_msg)  # must not raise
            await asyncio.sleep(0.05)

    asyncio.run(run())


def test_on_message_noop_when_no_handler_registered():
    """A message arriving before set_command_handler() has been called (or
    with none registered at all) must be silently ignored, not raise."""
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()
        msg = MagicMock(topic="chlorinator/testpool/action", payload=b'{"action": 2}')
        bridge._on_message(None, None, msg)  # no handler, no loop set - must not raise


def test_publish_state_failure_is_swallowed(monkeypatch, sample_data):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    fake_client.publish.side_effect = OSError("broker gone")
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge.publish_state(sample_data)  # must not raise


def test_disconnect_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    fake_client.disconnect.side_effect = OSError("already gone")
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge.disconnect()  # must not raise
