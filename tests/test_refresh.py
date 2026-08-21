import asyncio
from unittest.mock import AsyncMock, patch

from app import latest_state, refresh_now


def test_refresh_now_updates_state_and_publishes_on_success(sample_data):
    # poll_once returns sample_data itself (the same mutable dict refresh_now()
    # goes on to mutate in place via the resolve_chemistry_reading()
    # assignment), so snapshot the true pre-substitution values now, before
    # running - comparing against sample_data *after* the run would compare
    # the mutated dict to itself, trivially passing regardless of what was
    # actually passed to publish_state() at call time.
    original_ph = sample_data["ph_measurement"]
    original_chlorine_status = sample_data["chlorine_control_status"]

    # Mock.assert_called_once_with() stores a live reference to the call
    # argument, not a value snapshot - since publish_state() is called with
    # this same dict before it gets mutated later in the function, that
    # assertion would still pass even if a future refactor accidentally
    # reordered the two calls (sending MQTT the substituted reading instead
    # of the raw one). Capturing a real snapshot at call time via
    # side_effect is what actually catches that.
    published_snapshot = {}

    def capture_published_data(data):
        published_snapshot.update(data)

    with (
        patch("app.poll_once", new=AsyncMock(return_value=sample_data)),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        fake_bridge.publish_state.side_effect = capture_published_data
        # Real MqttBridge.resolve_chemistry_reading() would return the raw
        # (unsubstituted) reading here, matching valid chemistry in
        # sample_data - configure the mock to match, since refresh_now()
        # unpacks this into a 2-tuple.
        fake_bridge.resolve_chemistry_reading.return_value = (original_ph, original_chlorine_status)
        asyncio.run(refresh_now())

    assert latest_state["data"] == sample_data
    assert latest_state["error"] is None
    assert latest_state["updated_at"] is not None
    assert published_snapshot["ph_measurement"] == original_ph
    assert published_snapshot["chlorine_control_status"] == original_chlorine_status
    fake_bridge.publish_state.assert_called_once()
    fake_bridge.resolve_chemistry_reading.assert_called_once_with(sample_data)


def test_refresh_now_keeps_true_raw_reading_separate_from_substituted_data(sample_data):
    """The "Raw field dump" panel (render_raw_dump, fed by latest_state
    "raw_data") must show the device's actual reading even while
    latest_state["data"] carries a held/substituted one - otherwise the one
    tool built for diagnosing a stuck chemistry_values_valid window shows
    the same hidden-garbage problem it exists to reveal.

    Also the one test in this file where the raw and substituted values
    genuinely differ (7.4 vs original_ph) - the right place to prove
    mqtt_bridge.publish_state() is called with the raw reading, not the
    substituted one. Mock.assert_called_once_with() alone can't do that:
    it stores a live reference to the call argument, and poll_once returns
    the same mutable dict refresh_now() later mutates in place, so
    comparing that reference against sample_data *after* the run would
    just compare the mutated dict to itself - passing even if a future
    refactor accidentally reordered the two calls and sent MQTT the
    substituted 7.4 instead of the real reading. Capturing a snapshot via
    side_effect, at the moment publish_state() is actually called, is
    what would catch that."""
    # poll_once returns sample_data itself (mutated in place by refresh_now,
    # since it's the same object as `data`) - snapshot the true raw values
    # before running, rather than reading sample_data back afterward.
    original_ph = sample_data["ph_measurement"]
    original_chlorine_status = sample_data["chlorine_control_status"]

    published_snapshot = {}

    def capture_published_data(data):
        published_snapshot.update(data)

    with (
        patch("app.poll_once", new=AsyncMock(return_value=sample_data)),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        fake_bridge.publish_state.side_effect = capture_published_data
        # Simulate an invalid-chemistry poll where the bridge substitutes a
        # held reading different from the device's own raw one.
        fake_bridge.resolve_chemistry_reading.return_value = (7.4, "Ok")
        asyncio.run(refresh_now())

    assert latest_state["data"]["ph_measurement"] == 7.4
    assert latest_state["raw_data"]["ph_measurement"] == original_ph
    assert latest_state["raw_data"]["chlorine_control_status"] == original_chlorine_status
    assert published_snapshot["ph_measurement"] == original_ph
    assert published_snapshot["chlorine_control_status"] == original_chlorine_status


def test_refresh_now_records_error_on_poll_failure():
    with (
        patch("app.poll_once", new=AsyncMock(side_effect=RuntimeError("BLE scan failed"))),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        asyncio.run(refresh_now())

    assert latest_state["error"] == "BLE scan failed"
    assert latest_state["updated_at"] is not None
    fake_bridge.publish_state.assert_not_called()
