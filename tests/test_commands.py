import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pychlorinator.chlorinator_parsers import ChlorinatorActions

from app import handle_mqtt_command


@pytest.fixture(autouse=True)
def no_sleep_no_refresh(monkeypatch):
    """handle_mqtt_command sleeps COMMAND_SETTLE_DELAY_SECONDS then calls
    refresh_now() after a successful write - neither is relevant to testing
    the write logic itself. Zeroing the named constant (rather than
    patching asyncio.sleep itself, which would patch the real stdlib
    function globally since app.asyncio is the same module object) keeps
    the real sleep/event-loop machinery untouched."""
    monkeypatch.setattr("app.COMMAND_SETTLE_DELAY_SECONDS", 0)
    with patch("app.refresh_now", new=AsyncMock()) as fake_refresh:
        yield fake_refresh


def test_action_command_calls_write_action_with_correct_enum():
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    fake_write.assert_called_once_with(ChlorinatorActions.Auto)


def test_action_command_passes_through_extra_kwargs():
    """e.g. {"action": 11, "period_minutes": 30} for DisableAcidDosingForPeriod."""
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 11, "period_minutes": 30}))

    fake_write.assert_called_once_with(
        ChlorinatorActions.DisableAcidDosingForPeriod, period_minutes=30
    )


def test_setup_command_calls_write_setup_with_kwargs():
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_setup", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("setup", {"ph_control_setpoint": 7.4}))

    fake_write.assert_called_once_with(ph_control_setpoint=7.4)


def test_refresh_runs_after_a_successful_write(no_sleep_no_refresh):
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()),
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 1}))

    no_sleep_no_refresh.assert_called_once()


def test_command_ignored_when_device_not_found(no_sleep_no_refresh):
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    fake_write.assert_not_called()
    no_sleep_no_refresh.assert_not_called()  # no refresh if the write never happened


def test_write_failure_is_swallowed_and_skips_refresh(no_sleep_no_refresh):
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock(side_effect=RuntimeError("BLE error"))),
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))  # must not raise

    no_sleep_no_refresh.assert_not_called()


def test_unknown_action_value_is_swallowed(no_sleep_no_refresh):
    """999 isn't a valid ChlorinatorActions value - ChlorinatorActions(999)
    raises ValueError, which must be handled, not crash the app."""
    fake_device = object()
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)):
        asyncio.run(handle_mqtt_command("action", {"action": 999}))

    no_sleep_no_refresh.assert_not_called()


def test_unknown_command_kind_is_ignored(no_sleep_no_refresh):
    fake_device = object()
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)):
        asyncio.run(handle_mqtt_command("bogus", {}))

    no_sleep_no_refresh.assert_not_called()
