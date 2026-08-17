import asyncio
from unittest.mock import AsyncMock, patch

from app import latest_state, refresh_now


def test_refresh_now_updates_state_and_publishes_on_success(sample_data):
    with (
        patch("app.poll_once", new=AsyncMock(return_value=sample_data)),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        # Real MqttBridge.resolve_chemistry_reading() would return the raw
        # (unsubstituted) reading here, matching valid chemistry in
        # sample_data - configure the mock to match, since refresh_now()
        # unpacks this into a 2-tuple.
        fake_bridge.resolve_chemistry_reading.return_value = (
            sample_data["ph_measurement"],
            sample_data["chlorine_control_status"],
        )
        asyncio.run(refresh_now())

    assert latest_state["data"] == sample_data
    assert latest_state["error"] is None
    assert latest_state["updated_at"] is not None
    fake_bridge.publish_state.assert_called_once_with(sample_data)
    fake_bridge.resolve_chemistry_reading.assert_called_once_with(sample_data)


def test_refresh_now_keeps_true_raw_reading_separate_from_substituted_data(sample_data):
    """The "Raw field dump" panel (render_raw_dump, fed by latest_state
    "raw_data") must show the device's actual reading even while
    latest_state["data"] carries a held/substituted one - otherwise the one
    tool built for diagnosing a stuck chemistry_values_valid window shows
    the same hidden-garbage problem it exists to reveal."""
    # poll_once returns sample_data itself (mutated in place by refresh_now,
    # since it's the same object as `data`) - snapshot the true raw values
    # before running, rather than reading sample_data back afterward.
    original_ph = sample_data["ph_measurement"]
    original_chlorine_status = sample_data["chlorine_control_status"]
    with (
        patch("app.poll_once", new=AsyncMock(return_value=sample_data)),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        # Simulate an invalid-chemistry poll where the bridge substitutes a
        # held reading different from the device's own raw one.
        fake_bridge.resolve_chemistry_reading.return_value = (7.4, "Ok")
        asyncio.run(refresh_now())

    assert latest_state["data"]["ph_measurement"] == 7.4
    assert latest_state["raw_data"]["ph_measurement"] == original_ph
    assert latest_state["raw_data"]["chlorine_control_status"] == original_chlorine_status


def test_refresh_now_records_error_on_poll_failure():
    with (
        patch("app.poll_once", new=AsyncMock(side_effect=RuntimeError("BLE scan failed"))),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        asyncio.run(refresh_now())

    assert latest_state["error"] == "BLE scan failed"
    assert latest_state["updated_at"] is not None
    fake_bridge.publish_state.assert_not_called()
