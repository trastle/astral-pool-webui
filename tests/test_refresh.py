import asyncio
from unittest.mock import AsyncMock, patch

from app import latest_state, refresh_now


def test_refresh_now_updates_state_and_publishes_on_success(sample_data):
    with (
        patch("app.poll_once", new=AsyncMock(return_value=sample_data)),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        asyncio.run(refresh_now())

    assert latest_state["data"] == sample_data
    assert latest_state["error"] is None
    assert latest_state["updated_at"] is not None
    fake_bridge.publish_state.assert_called_once_with(sample_data)


def test_refresh_now_records_error_on_poll_failure():
    with (
        patch("app.poll_once", new=AsyncMock(side_effect=RuntimeError("BLE scan failed"))),
        patch("app.mqtt_bridge") as fake_bridge,
    ):
        asyncio.run(refresh_now())

    assert latest_state["error"] == "BLE scan failed"
    assert latest_state["updated_at"] is not None
    fake_bridge.publish_state.assert_not_called()
