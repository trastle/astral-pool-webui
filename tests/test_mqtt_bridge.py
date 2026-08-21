import asyncio
import json
from unittest.mock import MagicMock, patch

from pychlorinator.chlorinator_parsers import ChlorineControlStatuses

from conftest import FakeEnumValue
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


def test_build_state_payload_flattens_pump_timers(sample_data):
    payload = build_state_payload(sample_data)
    assert len(payload["pump_timers"]) == 4
    first = payload["pump_timers"][0]
    assert first == {
        "start_time": "10:00:00",
        "stop_time": "14:00:00",
        "enabled": True,
        "speed_level": 3,  # AI, per pychlorinator.SpeedLevels
    }
    assert all(isinstance(t["start_time"], str) for t in payload["pump_timers"])
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


def test_bridge_sets_username_and_password_when_configured(monkeypatch):
    """username_pw_set() happens in __init__, not connect() - covering it
    separately since every other test explicitly sets MQTT_USERNAME=None
    and never exercises this branch."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.MQTT_USERNAME", "pool")
    monkeypatch.setattr("mqtt_bridge.MQTT_PASSWORD", "hunter2")

    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        MqttBridge()

    fake_client.username_pw_set.assert_called_once_with("pool", "hunter2")


def test_bridge_skips_username_pw_set_when_no_username_configured(monkeypatch):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.MQTT_USERNAME", None)

    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        MqttBridge()

    fake_client.username_pw_set.assert_not_called()


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


def test_publish_state_holds_last_valid_reading_while_chemistry_invalid(monkeypatch, sample_data):
    """The device keeps returning a raw pH/ORP reading even while flagging
    chemistry_values_valid=False (observed: a flat 0.0 pH for hours after a
    power cycle) - the bridge should republish the last known-good reading
    instead of relaying that, while everything else (including the validity
    flags themselves) keeps updating live."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()

        bridge.publish_state(sample_data)
        good_payload = json.loads(fake_client.publish.call_args[0][1])
        assert good_payload["ph_measurement"] == 8.8
        assert good_payload["chlorine_control_status"] == 0

        invalid_data = {
            **sample_data,
            "ph_measurement": 0.0,
            "chlorine_control_status": FakeEnumValue("High", 2),
            "chemistry_values_valid": False,
            "chemistry_values_current": False,
        }
        bridge.publish_state(invalid_data)
        held_payload = json.loads(fake_client.publish.call_args[0][1])
        assert held_payload["ph_measurement"] == 8.8
        assert held_payload["chlorine_control_status"] == 0
        # The validity flags themselves still reflect reality, so this
        # state is still visible downstream - only the readings are held.
        assert held_payload["chemistry_values_valid"] is False
        assert held_payload["chemistry_values_current"] is False


def test_publish_state_passes_through_raw_reading_when_never_seen_a_valid_one(monkeypatch, sample_data):
    """No last-known-good value exists yet (e.g. a fresh start while the
    device is still stabilizing) - nothing better to substitute, so the raw
    (possibly junk) reading passes through rather than silently vanishing."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()

        invalid_data = {**sample_data, "ph_measurement": 0.0, "chemistry_values_valid": False}
        bridge.publish_state(invalid_data)
        payload = json.loads(fake_client.publish.call_args[0][1])
        assert payload["ph_measurement"] == 0.0


def test_resolve_chemistry_reading_returns_raw_values_when_valid(monkeypatch, sample_data):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()
        ph, status = bridge.resolve_chemistry_reading(sample_data)

    assert ph == sample_data["ph_measurement"]
    assert status is sample_data["chlorine_control_status"]


def test_resolve_chemistry_reading_substitutes_cached_reading_when_invalid(monkeypatch, sample_data):
    """The same protection app.py's dashboard/metrics need (see the
    module docstring on resolve_chemistry_reading) - and the returned
    status must be a real ChlorineControlStatuses member, not a plain
    int, since callers like app.py's humanize()/chlorine_level() expect
    whatever pychlorinator itself would have returned from a fresh poll.
    Uses an explicit real ("Ok", 4) reading rather than sample_data's
    default - its FakeEnumValue("Low", 0) doesn't match this fixture's
    real int-to-name mapping (0 is actually Invalid_NoMeasurement), which
    would make this test's intent confusing even though the reconstruction
    logic itself doesn't care what the value means."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    valid_data = {**sample_data, "ph_measurement": 7.4, "chlorine_control_status": FakeEnumValue("Ok", 4)}
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()
        bridge.publish_state(valid_data)  # establishes a valid cache first

        invalid_data = {**valid_data, "ph_measurement": 0.0, "chemistry_values_valid": False}
        ph, status = bridge.resolve_chemistry_reading(invalid_data)

    assert ph == 7.4
    assert status == ChlorineControlStatuses.Ok
    assert str(status) == "Ok"


def test_resolve_chemistry_reading_passes_through_raw_when_never_seen_a_valid_one(monkeypatch, sample_data):
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()

        invalid_data = {**sample_data, "ph_measurement": 0.0, "chemistry_values_valid": False}
        ph, status = bridge.resolve_chemistry_reading(invalid_data)

    assert ph == 0.0
    assert status is invalid_data["chlorine_control_status"]


def test_publish_state_persists_valid_reading_to_disk(monkeypatch, tmp_path, sample_data):
    """Every field the cache exists to protect - only pH and chlorine
    status (see mqtt_bridge.py's module docstring on LAST_KNOWN_GOOD_CACHE_FILE) - plus a
    last_changed timestamp, so the file is inspectable/debuggable on its
    own."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.LAST_KNOWN_GOOD_CACHE_FILE", tmp_path / "cache.json")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge.publish_state(sample_data)

    cached = json.loads((tmp_path / "cache.json").read_text())
    assert cached["ph_measurement"] == 8.8
    assert cached["chlorine_control_status"] == 0
    assert "last_changed" in cached


def test_publish_state_only_writes_disk_cache_when_the_value_changes(monkeypatch, tmp_path, sample_data):
    """The disk cache exists to survive a crash/restart, not to log every
    poll - repeating the same valid reading (the common case - most polls
    don't change anything) shouldn't touch disk each time."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.LAST_KNOWN_GOOD_CACHE_FILE", tmp_path / "cache.json")
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        bridge._save_cache_to_disk = MagicMock(wraps=bridge._save_cache_to_disk)

        bridge.publish_state(sample_data)
        bridge.publish_state(sample_data)  # identical reading again
        assert bridge._save_cache_to_disk.call_count == 1

        bridge.publish_state({**sample_data, "ph_measurement": 7.9})
        assert bridge._save_cache_to_disk.call_count == 2


def test_bridge_restores_last_known_good_reading_from_disk_on_startup(monkeypatch, tmp_path, sample_data):
    """The actual bug this exists to fix: a gateway restart landing while
    chemistry_values_valid is already False previously had nothing to fall
    back to (the in-memory cache doesn't survive a restart) - it would
    relay the device's raw junk reading (observed: a physically impossible
    0.0 pH) straight through until the device's own validity flag
    recovered, sometimes hours later. A fresh process should now recover
    the last known-good reading from disk before its very first poll."""
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({
        "ph_measurement": 7.3,
        "chlorine_control_status": 1,
        "last_changed": "2026-08-16T08:00:00+00:00",
    }))
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.LAST_KNOWN_GOOD_CACHE_FILE", cache_file)
    fake_client = MagicMock()
    with patch("mqtt_bridge.mqtt.Client", return_value=fake_client):
        bridge = MqttBridge()
        assert bridge._last_valid_ph_measurement == 7.3
        assert bridge._last_valid_chlorine_control_status == 1

        # First-ever publish_state() call for this process, and chemistry
        # is already invalid - previously nothing to fall back to.
        invalid_data = {**sample_data, "ph_measurement": 0.0, "chemistry_values_valid": False}
        bridge.publish_state(invalid_data)
        payload = json.loads(fake_client.publish.call_args[0][1])
        assert payload["ph_measurement"] == 7.3
        assert payload["chlorine_control_status"] == 1


def test_bridge_ignores_missing_cache_file_on_startup(monkeypatch, tmp_path):
    """First-ever run, or a fresh install - no cache file exists yet.
    Must behave exactly like before this feature existed, not raise."""
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.LAST_KNOWN_GOOD_CACHE_FILE", tmp_path / "does-not-exist.json")
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()

    assert bridge._last_valid_ph_measurement is None
    assert bridge._last_valid_chlorine_control_status is None


def test_bridge_ignores_corrupt_cache_file_on_startup(monkeypatch, tmp_path):
    """A write interrupted before the atomic rename in
    _save_cache_to_disk() shouldn't be possible, but tolerate a corrupt/
    malformed file anyway rather than crashing the app over a cache that
    only exists to make things more reliable."""
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("not valid json{")
    monkeypatch.setattr("mqtt_bridge.MQTT_HOST", "test-broker")
    monkeypatch.setattr("mqtt_bridge.LAST_KNOWN_GOOD_CACHE_FILE", cache_file)
    with patch("mqtt_bridge.mqtt.Client", return_value=MagicMock()):
        bridge = MqttBridge()  # must not raise

    assert bridge._last_valid_ph_measurement is None
    assert bridge._last_valid_chlorine_control_status is None


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
